import os
import json
import string
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs
import xml.etree.ElementTree as ET

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from google.cloud import firestore
from google.oauth2 import service_account
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
import requests

# --------------------------------------------------------------------
# CARREGAR VARIÁVEIS DO .env
# --------------------------------------------------------------------

load_dotenv()  # carrega MEUDANFE_API_KEY, SMTP_*, LICENCE_DAYS etc.

# --------------------------------------------------------------------
# CONFIGURAÇÕES FIRESTORE (LOCAL + KOYEB)
# --------------------------------------------------------------------

# 1) Para Koyeb: JSON inteiro do service account em variável de ambiente
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# 2) Para uso local: caminho do arquivo JSON
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS_JSON", "service-account.json"
)

# 3) Flag pra decidir se usa arquivo JSON em vez de credencial padrão
#    No Koyeb: USE_SERVICE_ACCOUNT_FILE=false
#    Local:    USE_SERVICE_ACCOUNT_FILE=true (e service-account.json no projeto)
USE_SERVICE_ACCOUNT_FILE = (
    os.getenv("USE_SERVICE_ACCOUNT_FILE", "true").lower() == "true"
)

db = None
try:
    if SERVICE_ACCOUNT_JSON:
        # Modo Koyeb: JSON inteiro em variável de ambiente
        print(
            "Usando credenciais do JSON da variável de ambiente GOOGLE_SERVICE_ACCOUNT_JSON"
        )
        info = json.loads(SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(info)
        project_id = info.get("project_id")
        db = firestore.Client(credentials=credentials, project=project_id)
        print(f"Firestore conectado ao projeto (JSON env): {project_id}")
    elif USE_SERVICE_ACCOUNT_FILE:
        # Modo desenvolvimento local (usa service-account.json)
        print(f"Tentando carregar credenciais do arquivo: {SERVICE_ACCOUNT_FILE}")
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE
        )
        db = firestore.Client(credentials=credentials, project=credentials.project_id)
        print(f"Firestore conectado ao projeto (arquivo): {credentials.project_id}")
    else:
        # Modo “ADC” (não deve ser usado no Koyeb, mais pra Cloud Run)
        print("Tentando usar credenciais padrão do Google (ADC)")
        db = firestore.Client()
        print("Firestore conectado usando ADC")
except Exception as e:
    print("ERRO ao criar cliente Firestore:", e)
    db = None

# --------------------------------------------------------------------
# CONFIGS GERAIS
# --------------------------------------------------------------------

# Api-Key central do MeuDanfe (uma só para todos os clientes)
MEUDANFE_API_KEY = os.getenv("MEUDANFE_API_KEY")
if not MEUDANFE_API_KEY:
    raise RuntimeError(
        "MEUDANFE_API_KEY não configurada na variável de ambiente ou .env"
    )

# Config de e-mail (se não tiver SMTP agora, não tem problema, o código trata)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER or "")

# Quantidade de dias de validade de cada licença
DEFAULT_LICENCE_DAYS = int(os.getenv("LICENCE_DAYS", "30"))

# Nome da coleção no Firestore (a que você já está usando)
LICENCES_COLLECTION = "sticky-notes"

# Dados da API clássica de notificação PagBank / PagSeguro
PAGBANK_EMAIL = os.getenv("PAGBANK_EMAIL")
PAGBANK_TOKEN = os.getenv("PAGBANK_TOKEN")
# endpoint padrão da notificação v3
PAGBANK_NOTIFICATION_BASE_URL = os.getenv(
    "PAGBANK_NOTIFICATION_BASE_URL",
    "https://ws.pagseguro.uol.com.br/v3/transactions/notifications",
)

if not PAGBANK_EMAIL or not PAGBANK_TOKEN:
    print(
        "ATENÇÃO: PAGBANK_EMAIL ou PAGBANK_TOKEN não configurados. "
        "Webhook não conseguirá consultar a notificação no PagBank."
    )

app = FastAPI(title="Backend de Licenças da Extensão NF")

# --------------------------------------------------------------------
# MODELOS P/ REQUISIÇÕES E RESPOSTAS
# --------------------------------------------------------------------


class LicencaValidarRequest(BaseModel):
    licenca: str  # exemplo: "TESTE-1234" (sem @#)


class LicencaValidarResponse(BaseModel):
    ok: bool
    motivo: Optional[str] = None
    expira_em: Optional[str] = None  # ISO 8601
    api_key_meudanfe: Optional[str] = None


# --------------------------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------------------------


def gerar_codigo_licenca(tamanho: int = 8) -> str:
    caracteres = string.ascii_uppercase + string.digits
    base = "".join(random.choice(caracteres) for _ in range(tamanho))
    return base[:4] + "-" + base[4:]


def criar_documento_licenca(
    codigo: str,
    email: str,
    cpf: Optional[str],
    id_transacao_pagbank: str,
    plano: str = "mensal",
) -> None:
    if db is None:
        raise RuntimeError("Firestore não inicializado corretamente")

    agora = datetime.now(timezone.utc)
    expira = agora + timedelta(days=DEFAULT_LICENCE_DAYS)

    doc_ref = db.collection(LICENCES_COLLECTION).document(codigo)
    doc_ref.set(
        {
            "email": email,
            "cpf": cpf,
            "status": "ativo",
            "compra_em": agora,
            "expira_em": expira,
            "plano": plano,
            "origem_pagamento": "pagbank",
            "id_transacao_pagbank": id_transacao_pagbank,
        }
    )


def enviar_email_licenca(
    para_email: str,
    codigo_licenca: str,
) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and FROM_EMAIL):
        print("SMTP não configurado. Dados da licença:")
        print(f"Destinatário: {para_email}")
        print(f"Licença: @#{codigo_licenca}")
        print(f"Api-Key MeuDanfe: @@{MEUDANFE_API_KEY}")
        return

    assunto = "Sua licença da extensão de NF"
    corpo = f"""
Olá!

Obrigado pela sua compra.

Aqui estão seus dados de acesso:

Chave da extensão (licença):
  @#{codigo_licenca}

Api-Key do MeuDanfe (não compartilhe):
  @@{MEUDANFE_API_KEY}

Como usar:
1. Instale a extensão no Chrome.
2. Abra o popup da extensão.
3. Em uma anotação, digite a linha com a licença:
   @#{codigo_licenca}
4. A extensão irá validar sua licença automaticamente.
5. A Api-Key do MeuDanfe será usada pelo sistema para baixar suas notas.

Validade da licença: {DEFAULT_LICENCE_DAYS} dias a partir da data da compra.

Qualquer dúvida, responda este e-mail.

Abraço!
"""

    msg = MIMEText(corpo, _charset="utf-8")
    msg["Subject"] = assunto
    msg["From"] = FROM_EMAIL
    msg["To"] = para_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def buscar_licenca(codigo: str) -> Optional[dict]:
    if db is None:
        raise RuntimeError("Firestore não inicializado corretamente")

    doc_ref = db.collection(LICENCES_COLLECTION).document(codigo)
    doc = doc_ref.get()
    if not doc.exists:
        return None
    return doc.to_dict()


def consultar_notificacao_pagbank(notification_code: str) -> Optional[dict]:
    """
    Usa o notificationCode para consultar a transação na API v3 do PagBank/PagSeguro.
    Retorna um dicionário com alguns campos importantes (status, email, cpf, transaction_id).
    Documentação: GET /v3/transactions/notifications/{notificationCode}?email=&token=
    """
    if not PAGBANK_EMAIL or not PAGBANK_TOKEN:
        print("PAGBANK_EMAIL ou PAGBANK_TOKEN não configurados; não dá pra consultar notificação.")
        return None

    url = f"{PAGBANK_NOTIFICATION_BASE_URL}/{notification_code}"
    params = {"email": PAGBANK_EMAIL, "token": PAGBANK_TOKEN}

    print("Consultando notificação PagBank:", url, params)

    try:
        resp = requests.get(url, params=params, timeout=15)
    except Exception as e:
        print("Erro HTTP consultando notificação PagBank:", e)
        return None

    print("Status HTTP PagBank:", resp.status_code)
    print("Resposta PagBank (XML):", resp.text)

    if resp.status_code != 200:
        return None

    # Parse XML de resposta
    try:
        root = ET.fromstring(resp.text)
    except Exception as e:
        print("Erro ao parsear XML do PagBank:", e)
        return None

    # XML padrão: <transaction>...</transaction>
    status_str = root.findtext(".//status")
    transaction_code = root.findtext(".//code")
    reference = root.findtext(".//reference")
    email_cliente = root.findtext(".//sender/email")

    # CPF (se existir)
    cpf = None
    doc_value = root.findtext(".//sender/documents/document/value")
    if doc_value:
        cpf = doc_value

    info = {
        "status": status_str,
        "transaction_code": transaction_code,
        "reference": reference,
        "email": email_cliente,
        "cpf": cpf,
    }
    print("Dados extraídos da notificação PagBank:", info)
    return info


def status_pagbank_e_pago(status_str: Optional[str]) -> bool:
    """
    Na API antiga (PagSeguro), os status são números:
    1 = Aguardando pagamento
    2 = Em análise
    3 = Paga
    4 = Disponível
    5 = Em disputa
    6 = Devolvida
    7 = Cancelada
    Vamos considerar 'pago' quando for 3 ou 4.
    """
    if not status_str:
        return False
    try:
        s = int(status_str)
    except ValueError:
        return False
    return s in (3, 4)


# --------------------------------------------------------------------
# ENDPOINT: WEBHOOK DO PAGBANK
# --------------------------------------------------------------------


@app.post("/pagbank/webhook")
async def pagbank_webhook(request: Request):
    """
    Webhook do PagBank.

    Fluxo:
    1. PagBank manda notificationCode / notificationType (form x-www-form-urlencoded)
    2. A gente lê e extrai o notificationCode
    3. Consulta a API de notificação do PagBank (v3) com esse código
    4. Se status da transação for pago, gera licença + salva no Firestore + manda e-mail
    """

    print("=== Webhook PagBank recebido ===")
    headers = dict(request.headers)
    print("Headers:", headers)

    content_type = headers.get("content-type", "")
    raw_body = await request.body()
    body_text = raw_body.decode(errors="ignore")
    print("Raw body:", body_text)

    data_form = {}
    if "application/x-www-form-urlencoded" in content_type and body_text:
        parsed = parse_qs(body_text)
        data_form = {
            k: (v[0] if isinstance(v, list) and v else v) for k, v in parsed.items()
        }

    notification_code = None
    notification_type = None

    if data_form:
        print("Payload FORM PagBank parseado:", data_form)
        notification_code = data_form.get("notificationCode") or data_form.get(
            "notification_code"
        )
        notification_type = data_form.get("notificationType") or data_form.get(
            "notification_type"
        )
        print("notificationCode:", notification_code, "notificationType:", notification_type)

    if not notification_code:
        # tentativa extra: se algum dia vier JSON
        try:
            data_json = await request.json()
            print("Payload JSON PagBank:", data_json)
            notification_code = data_json.get("notificationCode") or data_json.get(
                "notification_code"
            )
            notification_type = data_json.get("notificationType") or data_json.get(
                "notification_type"
            )
        except Exception as e:
            print("Erro ao ler JSON do PagBank:", e)

    if not notification_code:
        print("Nenhum notificationCode encontrado no webhook.")
        return JSONResponse(
            status_code=400,
            content={"detail": "notificationCode não encontrado no payload"},
        )

    # Consulta a notificação na API do PagBank
    info = consultar_notificacao_pagbank(notification_code)
    if not info:
        # Não vamos devolver erro 4xx pra não fazer o PagBank ficar reenviando.
        # Apenas logamos e retornamos 200 com info de erro.
        return {"ok": False, "motivo": "FALHA_CONSULTA_PAGBANK"}

    status_str = info.get("status")
    email_cliente = info.get("email")
    cpf_cliente = info.get("cpf")
    transaction_code = info.get("transaction_code") or notification_code

    if not email_cliente:
        # se por algum motivo o e-mail não vier, não gera licença para não ficar "vaga"
        print("E-mail do cliente não veio na notificação PagBank; não gerando licença.")
        return {"ok": False, "motivo": "SEM_EMAIL_CLIENTE"}

    if not status_pagbank_e_pago(status_str):
        print(f"Transação com status {status_str}, não é pago. Ignorando.")
        return {"ok": True, "ignored": True, "status": status_str}

    if db is None:
        print("Firestore não está inicializado, não foi possível salvar licença.")
        return {"ok": False, "motivo": "FIRESTORE_INDISPONIVEL"}

    # Gera código de licença único
    codigo = gerar_codigo_licenca()
    while db.collection(LICENCES_COLLECTION).document(codigo).get().exists:
        codigo = gerar_codigo_licenca()

    criar_documento_licenca(
        codigo=codigo,
        email=email_cliente,
        cpf=cpf_cliente,
        id_transacao_pagbank=transaction_code,
        plano="mensal",
    )

    enviar_email_licenca(
        para_email=email_cliente,
        codigo_licenca=codigo,
    )

    print("Licença gerada e salva no Firestore:", codigo)

    return {
        "ok": True,
        "licenca_gerada": codigo,
        "status_pagbank": status_str,
        "email": email_cliente,
    }


# --------------------------------------------------------------------
# ENDPOINT: VALIDAÇÃO DE LICENÇA (CHAMADO PELA EXTENSÃO)
# --------------------------------------------------------------------


@app.post("/licencas/validar", response_model=LicencaValidarResponse)
async def validar_licenca(body: LicencaValidarRequest):
    codigo = body.licenca.strip().upper()

    if codigo.startswith("@#"):
        codigo = codigo[2:].strip().upper()

    lic = buscar_licenca(codigo)
    if lic is None:
        return LicencaValidarResponse(
            ok=False,
            motivo="LICENCA_INEXISTENTE",
        )

    status = lic.get("status", "ativo")
    if status != "ativo":
        return LicencaValidarResponse(
            ok=False,
            motivo=f"LICENCA_{status.upper()}",
        )

    expira_em = lic.get("expira_em")
    agora = datetime.now(timezone.utc)

    expira_dt: Optional[datetime] = None

    if isinstance(expira_em, datetime):
        expira_dt = expira_em

    if expira_dt and expira_dt < agora:
        if db is not None:
            db.collection(LICENCES_COLLECTION).document(codigo).update(
                {"status": "expirado"}
            )

        return LicencaValidarResponse(
            ok=False,
            motivo="LICENCA_EXPIRADA",
            expira_em=expira_dt.isoformat(),
        )

    return LicencaValidarResponse(
        ok=True,
        expira_em=expira_dt.isoformat() if expira_dt else None,
        api_key_meudanfe=None,
    )


# --------------------------------------------------------------------
# ENDPOINT SIMPLES SÓ PRA TESTAR SE A API ESTÁ NO AR
# --------------------------------------------------------------------


@app.get("/")
async def root():
    return {"status": "ok", "mensagem": "Backend de licenças rodando"}
