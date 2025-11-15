import os
import json
import string
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from google.cloud import firestore
from google.oauth2 import service_account
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText

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
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "service-account.json")

# 3) Flag pra decidir se usa arquivo JSON em vez de credencial padrão
USE_SERVICE_ACCOUNT_FILE = os.getenv("USE_SERVICE_ACCOUNT_FILE", "true").lower() == "true"

db = None
try:
    if SERVICE_ACCOUNT_JSON:
        # Modo Koyeb: JSON inteiro em variável de ambiente
        print("Usando credenciais do JSON da variável de ambiente GOOGLE_SERVICE_ACCOUNT_JSON")
        info = json.loads(SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(info)
        project_id = info.get("project_id")
        db = firestore.Client(credentials=credentials, project=project_id)
        print(f"Firestore conectado ao projeto (JSON env): {project_id}")
    elif USE_SERVICE_ACCOUNT_FILE:
        # Modo desenvolvimento local (usa service-account.json)
        print(f"Tentando carregar credenciais do arquivo: {SERVICE_ACCOUNT_FILE}")
        credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
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
    raise RuntimeError("MEUDANFE_API_KEY não configurada na variável de ambiente ou .env")

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


class PagBankWebhookPayload(BaseModel):
    status: Optional[str] = None
    transaction_id: Optional[str] = None
    id: Optional[str] = None
    customer: dict = {}


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


# --------------------------------------------------------------------
# ENDPOINT: WEBHOOK DO PAGBANK
# --------------------------------------------------------------------

@app.post("/pagbank/webhook")
async def pagbank_webhook(payload: PagBankWebhookPayload):
    print("Webhook PagBank recebido:", payload.dict())

    status_pagamento = payload.status
    id_transacao = payload.transaction_id or payload.id

    dados_cliente = payload.customer or {}
    email_cliente = dados_cliente.get("email")
    cpf_cliente = dados_cliente.get("tax_id") or dados_cliente.get("cpf")

    if status_pagamento is None or id_transacao is None or email_cliente is None:
        return JSONResponse(
            status_code=400,
            content={"detail": "Payload do PagBank incompleto"},
        )

    if str(status_pagamento).upper() not in ("PAID", "APPROVED"):
        return {"ok": True, "ignored": True}

    if db is None:
        raise RuntimeError("Firestore não inicializado corretamente")

    codigo = gerar_codigo_licenca()
    while db.collection(LICENCES_COLLECTION).document(codigo).get().exists:
        codigo = gerar_codigo_licenca()

    criar_documento_licenca(
        codigo=codigo,
        email=email_cliente,
        cpf=cpf_cliente,
        id_transacao_pagbank=id_transacao,
        plano="mensal",
    )

    enviar_email_licenca(
        para_email=email_cliente,
        codigo_licenca=codigo,
    )

    return {"ok": True, "licenca_gerada": codigo}


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
