import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from time import monotonic
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from supabase import Client, create_client


logger = logging.getLogger("roleta_api")

ORIGEM_OFICIAL = "https://tnlabs-dev.github.io/"
CODIGO_CURTO_PATTERN = re.compile(r"^[0-9]{6}$")
TOKEN_LEGADO_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
VOUCHER_PREFIX_PATTERN = re.compile(r"^[A-Z0-9]{2,8}$")
MENSAGEM_BANCO_INDISPONIVEL = (
    "O serviço está temporariamente indisponível. Tente novamente em instantes."
)
MENSAGEM_SESSAO_INVALIDA = "Sua sessão administrativa expirou. Entre novamente."


@dataclass(frozen=True)
class Settings:
    """Configurações lidas do ambiente, sem expor os segredos nos logs."""

    supabase_url: str
    supabase_key: str
    senha_funcionaria: str
    senha_admin: str
    admin_session_secret: str
    origens_permitidas: tuple[str, ...] = (ORIGEM_OFICIAL,)
    admin_session_seconds: int = 8 * 60 * 60
    api_display_name: str = "Roleta API"
    voucher_prefix: str = "CPR"
    codigo_validade_minutos: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        valores = {
            "SUPABASE_URL": os.environ.get("SUPABASE_URL", "").strip(),
            "SUPABASE_KEY": os.environ.get("SUPABASE_KEY", "").strip(),
            "SENHA_FUNCIONARIA": os.environ.get("SENHA_FUNCIONARIA", ""),
            "SENHA_ADMIN": os.environ.get("SENHA_ADMIN", ""),
            "ADMIN_SESSION_SECRET": os.environ.get("ADMIN_SESSION_SECRET", ""),
        }
        ausentes = [nome for nome, valor in valores.items() if not valor]
        if ausentes:
            raise RuntimeError(
                "Variáveis obrigatórias não configuradas: " + ", ".join(ausentes)
            )

        origens_texto = os.environ.get("ORIGENS_PERMITIDAS", ORIGEM_OFICIAL)
        origens = tuple(
            origem.strip().rstrip("/")
            for origem in origens_texto.split(",")
            if origem.strip()
        )
        if not origens:
            raise RuntimeError("Configure ao menos uma origem em ORIGENS_PERMITIDAS.")

        api_display_name = os.environ.get("API_DISPLAY_NAME", "Roleta API").strip()
        if not api_display_name or len(api_display_name) > 80:
            raise RuntimeError("API_DISPLAY_NAME precisa ter entre 1 e 80 caracteres.")

        voucher_prefix = os.environ.get("VOUCHER_PREFIX", "CPR").strip().upper()
        if not VOUCHER_PREFIX_PATTERN.fullmatch(voucher_prefix):
            raise RuntimeError(
                "VOUCHER_PREFIX deve conter de 2 a 8 letras maiúsculas ou números."
            )

        validade_texto = os.environ.get("CODIGO_VALIDADE_MINUTOS", "30").strip()
        try:
            codigo_validade_minutos = int(validade_texto)
        except ValueError as erro:
            raise RuntimeError(
                "CODIGO_VALIDADE_MINUTOS deve ser um número inteiro."
            ) from erro
        if not 5 <= codigo_validade_minutos <= 120:
            raise RuntimeError(
                "CODIGO_VALIDADE_MINUTOS deve ficar entre 5 e 120 minutos."
            )

        return cls(
            supabase_url=valores["SUPABASE_URL"],
            supabase_key=valores["SUPABASE_KEY"],
            senha_funcionaria=valores["SENHA_FUNCIONARIA"],
            senha_admin=valores["SENHA_ADMIN"],
            admin_session_secret=valores["ADMIN_SESSION_SECRET"],
            origens_permitidas=origens,
            api_display_name=api_display_name,
            voucher_prefix=voucher_prefix,
            codigo_validade_minutos=codigo_validade_minutos,
        )


class ParticipanteInput(BaseModel):
    nome: str
    whatsapp: str
    ciencia_privacidade: bool
    data_nascimento: date | None = None
    consentimento_aniversario: bool = False

    @field_validator("nome")
    @classmethod
    def validar_nome(cls, valor: str) -> str:
        valor_normalizado = " ".join(valor.split())
        if len(valor_normalizado) < 2 or len(valor_normalizado) > 100:
            raise ValueError("Digite um nome válido.")
        return valor_normalizado

    @field_validator("whatsapp")
    @classmethod
    def validar_whatsapp(cls, valor: str) -> str:
        somente_numeros = re.sub(r"\D", "", valor)
        if not re.fullmatch(r"\d{10,11}", somente_numeros):
            raise ValueError(
                "WhatsApp inválido. Use o formato (DD) 9XXXX-XXXX, com DDD."
            )
        return somente_numeros

    @field_validator("ciencia_privacidade")
    @classmethod
    def validar_ciencia_privacidade(cls, valor: bool) -> bool:
        if valor is not True:
            raise ValueError(
                "Você precisa confirmar que leu o Aviso de Privacidade."
            )
        return valor

    @model_validator(mode="after")
    def validar_programa_aniversario(self) -> "ParticipanteInput":
        if self.consentimento_aniversario and self.data_nascimento is None:
            raise ValueError(
                "Informe a data de nascimento para autorizar a mensagem de aniversário."
            )
        if self.data_nascimento is not None and not self.consentimento_aniversario:
            raise ValueError(
                "Marque a autorização de aniversário para enviar a data de nascimento."
            )
        if self.data_nascimento is not None:
            hoje = date.today()
            idade = hoje.year - self.data_nascimento.year - (
                (hoje.month, hoje.day)
                < (self.data_nascimento.month, self.data_nascimento.day)
            )
            if idade < 18:
                raise ValueError(
                    "O cadastro de aniversário está disponível apenas para maiores de 18 anos."
                )
        return self


class GerarConviteInput(BaseModel):
    senha: str = Field(min_length=1, max_length=128)


class AdminLoginInput(BaseModel):
    senha: str = Field(min_length=1, max_length=256)


class LimparParticipantesInput(BaseModel):
    confirmacao: str = Field(min_length=7, max_length=7)
    total_esperado: int = Field(ge=0, le=10_000_000)

    @field_validator("confirmacao")
    @classmethod
    def validar_confirmacao(cls, valor: str) -> str:
        if valor != "EXCLUIR":
            raise ValueError("Digite EXCLUIR exatamente como solicitado.")
        return valor


class CampanhaBaseInput(BaseModel):
    nome: str = Field(min_length=3, max_length=100)
    data_inicio: datetime | None = None
    data_fim: datetime | None = None
    texto_consentimento: str = Field(min_length=10, max_length=500)

    @field_validator("nome", "texto_consentimento")
    @classmethod
    def normalizar_texto(cls, valor: str) -> str:
        return " ".join(valor.split())

    @model_validator(mode="after")
    def validar_periodo(self) -> "CampanhaBaseInput":
        if self.data_inicio and self.data_fim and self.data_fim <= self.data_inicio:
            raise ValueError("O encerramento deve ser posterior ao início.")
        return self


class CampanhaCriarInput(CampanhaBaseInput):
    pass


class CampanhaAtualizarInput(CampanhaBaseInput):
    status: Literal["rascunho", "ativa", "encerrada", "cancelada"]


class PremioBaseInput(BaseModel):
    nome: str = Field(min_length=2, max_length=100)
    estoque_disponivel: int = Field(ge=0, le=1_000_000)
    peso_sorteio: Decimal = Field(gt=0, le=1_000_000)
    ativo: bool = True

    @field_validator("nome")
    @classmethod
    def normalizar_nome(cls, valor: str) -> str:
        return " ".join(valor.split())


class PremioCriarInput(PremioBaseInput):
    campanha_id: int = Field(gt=0)


class PremioAtualizarInput(PremioBaseInput):
    pass


class ResgateInput(BaseModel):
    resgatado: bool
    observacao: str | None = Field(default=None, max_length=500)

    @field_validator("observacao")
    @classmethod
    def normalizar_observacao(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        normalizado = " ".join(valor.split())
        return normalizado or None


class InMemoryRateLimiter:
    """Proteção simples por IP e por rota para uma única instância da API."""

    def __init__(self, janela_segundos: int = 60) -> None:
        self.janela_segundos = janela_segundos
        self._tentativas: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def verificar(self, request: Request, categoria: str, limite: int) -> None:
        ip = request.client.host if request.client else "desconhecido"
        chave = (categoria, ip)
        agora = monotonic()

        with self._lock:
            tentativas = self._tentativas[chave]
            while tentativas and agora - tentativas[0] >= self.janela_segundos:
                tentativas.popleft()

            if len(tentativas) >= limite:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "Muitas tentativas em pouco tempo. "
                        "Aguarde um minuto e tente novamente."
                    ),
                )
            tentativas.append(agora)


def validar_formato_codigo(codigo: str, aceitar_legado: bool = True) -> None:
    codigo_valido = CODIGO_CURTO_PATTERN.fullmatch(codigo)
    token_legado_valido = aceitar_legado and TOKEN_LEGADO_PATTERN.fullmatch(codigo)
    if not codigo_valido and not token_legado_valido:
        raise HTTPException(status_code=404, detail="Código não existe!")


def executar_consulta(operacao):
    """Centraliza falhas do Supabase e evita devolver detalhes internos."""

    try:
        return operacao()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Falha em uma operação com o Supabase")
        raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)


def primeira_linha(resposta: Any) -> dict[str, Any] | None:
    dados = getattr(resposta, "data", None)
    if not dados:
        return None
    if isinstance(dados, dict):
        return dados
    if isinstance(dados, list) and isinstance(dados[0], dict):
        return dados[0]
    return None


def codificar_base64(conteudo: bytes) -> str:
    return base64.urlsafe_b64encode(conteudo).decode("ascii").rstrip("=")


def decodificar_base64(conteudo: str) -> bytes:
    return base64.urlsafe_b64decode(conteudo + "=" * (-len(conteudo) % 4))


def criar_sessao_admin(settings: Settings, agora: int | None = None) -> tuple[str, int]:
    emitido_em = int(time.time()) if agora is None else agora
    expira_em = emitido_em + settings.admin_session_seconds
    payload = codificar_base64(
        json.dumps(
            {"sub": "cappri-admin", "iat": emitido_em, "exp": expira_em},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assinatura = codificar_base64(
        hmac.new(
            settings.admin_session_secret.encode("utf-8"),
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"{payload}.{assinatura}", expira_em


def validar_sessao_admin(
    token: str, settings: Settings, agora: int | None = None
) -> bool:
    try:
        payload_codificado, assinatura_recebida = token.split(".", 1)
        assinatura_correta = codificar_base64(
            hmac.new(
                settings.admin_session_secret.encode("utf-8"),
                payload_codificado.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not secrets.compare_digest(assinatura_recebida, assinatura_correta):
            return False
        payload = json.loads(decodificar_base64(payload_codificado))
        instante = int(time.time()) if agora is None else agora
        return (
            payload.get("sub") == "cappri-admin"
            and isinstance(payload.get("exp"), int)
            and payload["exp"] > instante
        )
    except (
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return False


def proteger_celula_csv(valor: Any) -> str:
    texto = "" if valor is None else str(valor)
    if texto.startswith(("=", "+", "-", "@")):
        return "'" + texto
    return texto


def create_app(
    settings: Settings | None = None,
    database: Client | Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = database or create_client(settings.supabase_url, settings.supabase_key)

    api = FastAPI(title=settings.api_display_name, version="6.1.0")
    api.state.settings = settings
    api.state.database = database
    api.state.rate_limiter = InMemoryRateLimiter()

    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.origens_permitidas),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
        expose_headers=["Content-Disposition", "X-Total-Registros"],
    )

    @api.middleware("http")
    async def impedir_cache_admin(request: Request, call_next):
        resposta = await call_next(request)
        if request.url.path.startswith("/admin"):
            resposta.headers["Cache-Control"] = "no-store"
        return resposta

    @api.exception_handler(RequestValidationError)
    async def tratar_erro_de_validacao(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        erros = exc.errors()
        mensagem = erros[0]["msg"] if erros else "Dados inválidos."
        mensagem = mensagem.replace("Value error, ", "")
        return JSONResponse(status_code=422, content={"detail": mensagem})

    def exigir_admin(request: Request) -> None:
        autorizacao = request.headers.get("Authorization", "")
        prefixo = "Bearer "
        if not autorizacao.startswith(prefixo) or not validar_sessao_admin(
            autorizacao[len(prefixo) :], settings
        ):
            raise HTTPException(status_code=401, detail=MENSAGEM_SESSAO_INVALIDA)

    def gerar_codigo_convite() -> dict[str, Any]:
        expira_em = datetime.now(timezone.utc) + timedelta(
            minutes=settings.codigo_validade_minutos
        )

        for _ in range(12):
            token_interno = secrets.token_urlsafe(24)
            codigo_curto = f"{secrets.randbelow(1_000_000):06d}"
            resposta = executar_consulta(
                lambda: database.rpc(
                    "gerar_convite_roleta",
                    {
                        "p_token": token_interno,
                        "p_codigo_curto": codigo_curto,
                        "p_expira_em": expira_em.isoformat(),
                    },
                ).execute()
            )
            resultado = primeira_linha(resposta)
            if resultado is None:
                logger.error("RPC gerar_convite_roleta não devolveu resultado")
                raise HTTPException(
                    status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL
                )
            if resultado.get("resultado") == "codigo_em_uso":
                continue
            if resultado.get("resultado") == "sem_campanha":
                raise HTTPException(
                    status_code=409, detail="Não existe uma campanha ativa."
                )
            if resultado.get("resultado") != "sucesso":
                logger.error("RPC gerar_convite_roleta devolveu estado desconhecido")
                raise HTTPException(
                    status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL
                )

            codigo_gerado = resultado.get("codigo_gerado")
            if not isinstance(codigo_gerado, str) or not CODIGO_CURTO_PATTERN.fullmatch(
                codigo_gerado
            ):
                logger.error("RPC gerar_convite_roleta devolveu código inválido")
                raise HTTPException(
                    status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL
                )

            expiracao_retornada = resultado.get("expira_em") or expira_em.isoformat()
            return {
                "codigo": codigo_gerado,
                "token": codigo_gerado,
                "campanha": resultado["campanha"],
                "expira_em": expiracao_retornada,
                "validade_minutos": settings.codigo_validade_minutos,
            }

        logger.error("Não foi possível reservar um código curto após várias tentativas")
        raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

    def executar_rpc_admin(
        nome: str,
        parametros: dict[str, Any],
        erros_conhecidos: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        resposta = executar_consulta(
            lambda: database.rpc(nome, parametros).execute()
        )
        resultado = primeira_linha(resposta)
        if resultado is None:
            logger.error("RPC administrativa %s não devolveu resultado", nome)
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)
        codigo = resultado.get("resultado")
        if codigo != "sucesso":
            mensagem = (erros_conhecidos or {}).get(str(codigo))
            if mensagem:
                raise HTTPException(status_code=409, detail=mensagem)
            logger.error("RPC administrativa %s devolveu %s", nome, codigo)
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)
        return resultado

    @api.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "acordado"}

    @api.post("/gerar-convite")
    def gerar_convite(dados: GerarConviteInput, request: Request) -> dict[str, Any]:
        api.state.rate_limiter.verificar(request, "gerar-convite", limite=5)

        senha_enviada = dados.senha.encode("utf-8")
        senha_correta = settings.senha_funcionaria.encode("utf-8")
        if not secrets.compare_digest(senha_enviada, senha_correta):
            raise HTTPException(status_code=401, detail="Senha incorreta.")
        return gerar_codigo_convite()

    @api.get("/premios")
    def listar_premios() -> dict[str, Any]:
        resposta = executar_consulta(
            lambda: database.rpc("listar_premios_roleta", {}).execute()
        )
        linhas = getattr(resposta, "data", None)
        if not linhas:
            logger.error("RPC listar_premios_roleta não devolveu resultado")
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        primeiro = linhas[0]
        if primeiro.get("resultado") == "sem_campanha":
            raise HTTPException(status_code=409, detail="Não existe uma campanha ativa.")
        if primeiro.get("resultado") == "sem_premios":
            raise HTTPException(status_code=409, detail="A campanha não possui prêmios.")
        if primeiro.get("resultado") != "sucesso":
            logger.error("RPC listar_premios_roleta devolveu estado desconhecido")
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        premios = [
            {
                "id": linha["premio_id"],
                "nome": linha["premio_nome"],
                "posicao_roleta": linha["posicao_roleta"],
            }
            for linha in linhas
            if linha.get("premio_id") is not None
        ]
        return {
            "status": "sucesso",
            "campanha": {
                "id": primeiro["campanha_id"],
                "nome": primeiro["campanha_nome"],
                "texto_consentimento": primeiro.get("texto_consentimento"),
                "politica_privacidade_versao": primeiro.get(
                    "politica_privacidade_versao"
                ),
            },
            "dados": premios,
        }

    def consultar_codigo(codigo: str, request: Request) -> dict[str, Any]:
        validar_formato_codigo(codigo)
        api.state.rate_limiter.verificar(request, "verificar-codigo", limite=10)

        resposta = executar_consulta(
            lambda: database.rpc(
                "verificar_token_roleta", {"p_token": codigo}
            ).execute()
        )
        resultado = primeira_linha(resposta)
        if resultado is None or not isinstance(resultado.get("valido"), bool):
            logger.error("RPC verificar_token_roleta não devolveu resultado válido")
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        retorno: dict[str, Any] = {"valido": resultado["valido"]}
        if resultado.get("mensagem"):
            retorno["mensagem"] = resultado["mensagem"]
        if resultado.get("campanha"):
            retorno["campanha"] = resultado["campanha"]
        if resultado.get("expira_em"):
            retorno["expira_em"] = resultado["expira_em"]
        return retorno

    @api.get("/verificar-codigo/{codigo}")
    def verificar_codigo(codigo: str, request: Request) -> dict[str, Any]:
        validar_formato_codigo(codigo, aceitar_legado=False)
        return consultar_codigo(codigo, request)

    @api.get("/verificar-token/{token}")
    def verificar_token_legado(token: str, request: Request) -> dict[str, Any]:
        return consultar_codigo(token, request)

    @api.post("/sortear/{token}")
    def sortear_premio(
        token: str, dados: ParticipanteInput, request: Request
    ) -> dict[str, Any]:
        validar_formato_codigo(token)
        api.state.rate_limiter.verificar(request, "sortear", limite=5)

        resposta = executar_consulta(
            lambda: database.rpc(
                "sortear_premio_com_privacidade",
                {
                    "p_token": token,
                    "p_nome": dados.nome,
                    "p_whatsapp": dados.whatsapp,
                    "p_ciencia_privacidade": dados.ciencia_privacidade,
                    "p_data_nascimento": dados.data_nascimento.isoformat()
                    if dados.data_nascimento
                    else None,
                    "p_consentimento_aniversario": (
                        dados.consentimento_aniversario
                    ),
                },
            ).execute()
        )
        resultado = primeira_linha(resposta)
        if resultado is None:
            logger.error(
                "RPC sortear_premio_com_privacidade não devolveu resultado"
            )
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        codigo = resultado.get("resultado")
        erros_conhecidos = {
            "token_invalido": (404, "Código não existe!"),
            "token_utilizado": (409, "Este código já foi utilizado!"),
            "token_cancelado": (409, "Este código foi cancelado."),
            "codigo_expirado": (
                409,
                "Este código expirou. Peça um novo código no caixa.",
            ),
            "campanha_inativa": (409, "Esta campanha não está ativa."),
            "sem_premios": (409, "Acabaram os prêmios no estoque!"),
        }
        if codigo in erros_conhecidos:
            status_code, detalhe = erros_conhecidos[codigo]
            raise HTTPException(status_code=status_code, detail=detalhe)
        if codigo != "sucesso":
            logger.error(
                "RPC sortear_premio_com_privacidade devolveu estado desconhecido"
            )
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        participante_id = resultado.get("participante_id")
        if not isinstance(participante_id, int) or participante_id <= 0:
            logger.error(
                "RPC sortear_premio_com_privacidade devolveu participante inválido"
            )
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        return {
            "status": "sucesso",
            "mensagem": resultado["mensagem"],
            "premio": resultado["premio"],
            "indice_roleta": resultado["indice_roleta"],
            "codigo_voucher": f"{settings.voucher_prefix}-{participante_id:06d}",
        }

    @api.post("/admin/login")
    def login_admin(dados: AdminLoginInput, request: Request) -> dict[str, Any]:
        api.state.rate_limiter.verificar(request, "admin-login", limite=5)
        if not secrets.compare_digest(
            dados.senha.encode("utf-8"), settings.senha_admin.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Senha administrativa incorreta.")

        token, expira_em = criar_sessao_admin(settings)
        return {
            "token": token,
            "expira_em": datetime.fromtimestamp(expira_em, tz=timezone.utc).isoformat(),
            "duracao_segundos": settings.admin_session_seconds,
        }

    @api.get("/admin/sessao")
    def verificar_sessao_admin(request: Request) -> dict[str, bool]:
        exigir_admin(request)
        return {"autenticado": True}

    @api.get("/admin/painel")
    def obter_painel_admin(
        request: Request, campanha_id: int | None = None
    ) -> dict[str, Any]:
        exigir_admin(request)
        resposta = executar_consulta(
            lambda: database.rpc(
                "obter_painel_admin", {"p_campanha_id": campanha_id}
            ).execute()
        )
        resultado = primeira_linha(resposta)
        if resultado is None or not isinstance(resultado.get("painel"), dict):
            logger.error("RPC obter_painel_admin não devolveu resultado válido")
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)
        painel = resultado["painel"]
        if painel.get("resultado") == "campanha_nao_encontrada":
            raise HTTPException(status_code=404, detail="Campanha não encontrada.")

        campanha = painel.get("campanha")
        campanha_selecionada = campanha.get("id") if isinstance(campanha, dict) else None
        if isinstance(campanha_selecionada, int):
            resposta_convites = executar_consulta(
                lambda: database.rpc(
                    "obter_convites_admin",
                    {"p_campanha_id": campanha_selecionada},
                ).execute()
            )
            linha_convites = primeira_linha(resposta_convites)
            resumo_convites = (
                linha_convites.get("resumo")
                if isinstance(linha_convites, dict)
                else None
            )
            if not isinstance(resumo_convites, dict):
                logger.error("RPC obter_convites_admin não devolveu resultado válido")
                raise HTTPException(
                    status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL
                )
            painel["convites"] = resumo_convites.get("convites", [])
            metricas = painel.setdefault("metricas", {})
            if isinstance(metricas, dict) and isinstance(
                resumo_convites.get("metricas"), dict
            ):
                metricas.update(resumo_convites["metricas"])
        return painel

    @api.post("/admin/convites")
    def gerar_convite_admin(request: Request) -> dict[str, Any]:
        exigir_admin(request)
        api.state.rate_limiter.verificar(request, "admin-convites", limite=20)
        return gerar_codigo_convite()

    @api.patch("/admin/convites/{convite_id}/cancelar")
    def cancelar_convite_admin(convite_id: int, request: Request) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "cancelar_convite_admin",
            {"p_acesso_id": convite_id},
            {
                "nao_encontrado": "Convite não encontrado.",
                "ja_utilizado": "Um convite utilizado não pode ser cancelado.",
                "ja_cancelado": "Este convite já estava cancelado.",
            },
        )

    @api.post("/admin/campanhas")
    def criar_campanha_admin(
        dados: CampanhaCriarInput, request: Request
    ) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "criar_campanha_admin",
            {
                "p_nome": dados.nome,
                "p_data_inicio": dados.data_inicio.isoformat()
                if dados.data_inicio
                else None,
                "p_data_fim": dados.data_fim.isoformat() if dados.data_fim else None,
                "p_texto_consentimento": dados.texto_consentimento,
            },
        )

    @api.put("/admin/campanhas/{campanha_id}")
    def atualizar_campanha_admin(
        campanha_id: int, dados: CampanhaAtualizarInput, request: Request
    ) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "atualizar_campanha_admin",
            {
                "p_campanha_id": campanha_id,
                "p_nome": dados.nome,
                "p_status": dados.status,
                "p_data_inicio": dados.data_inicio.isoformat()
                if dados.data_inicio
                else None,
                "p_data_fim": dados.data_fim.isoformat() if dados.data_fim else None,
                "p_texto_consentimento": dados.texto_consentimento,
            },
            {
                "nao_encontrada": "Campanha não encontrada.",
                "outra_campanha_ativa": (
                    "Encerre a campanha ativa antes de ativar esta campanha."
                ),
                "premios_insuficientes": (
                    "Cadastre e ative pelo menos dois prêmios antes de ativar."
                ),
            },
        )

    @api.post("/admin/premios")
    def criar_premio_admin(dados: PremioCriarInput, request: Request) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "criar_premio_admin",
            {
                "p_campanha_id": dados.campanha_id,
                "p_nome": dados.nome,
                "p_estoque_disponivel": dados.estoque_disponivel,
                "p_peso_sorteio": str(dados.peso_sorteio),
                "p_ativo": dados.ativo,
            },
            {"campanha_nao_encontrada": "Campanha não encontrada."},
        )

    @api.put("/admin/premios/{premio_id}")
    def atualizar_premio_admin(
        premio_id: int, dados: PremioAtualizarInput, request: Request
    ) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "atualizar_premio_admin",
            {
                "p_premio_id": premio_id,
                "p_nome": dados.nome,
                "p_estoque_disponivel": dados.estoque_disponivel,
                "p_peso_sorteio": str(dados.peso_sorteio),
                "p_ativo": dados.ativo,
            },
            {
                "nao_encontrado": "Prêmio não encontrado.",
                "minimo_dois_premios": (
                    "Uma campanha ativa precisa manter pelo menos dois prêmios ativos."
                ),
            },
        )

    @api.patch("/admin/participantes/{participante_id}/resgate")
    def atualizar_resgate_admin(
        participante_id: int, dados: ResgateInput, request: Request
    ) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "atualizar_resgate_admin",
            {
                "p_participante_id": participante_id,
                "p_resgatado": dados.resgatado,
                "p_observacao": dados.observacao,
            },
            {"nao_encontrado": "Participante não encontrada."},
        )

    @api.patch("/admin/participantes/{participante_id}/revogar-aniversario")
    def revogar_aniversario_admin(
        participante_id: int, request: Request
    ) -> dict[str, Any]:
        exigir_admin(request)
        return executar_rpc_admin(
            "revogar_consentimento_aniversario_admin",
            {"p_participante_id": participante_id},
            {
                "nao_encontrado": "Participante não encontrada.",
                "nao_autorizado": (
                    "Esta participante não autorizou mensagens de aniversário."
                ),
                "ja_revogado": "A autorização de aniversário já foi revogada.",
            },
        )

    @api.delete("/admin/participantes")
    def limpar_participantes_admin(
        dados: LimparParticipantesInput, request: Request, campanha_id: int
    ) -> dict[str, Any]:
        exigir_admin(request)
        api.state.rate_limiter.verificar(request, "admin-limpeza", limite=3)
        resultado = executar_rpc_admin(
            "limpar_participantes_admin",
            {
                "p_campanha_id": campanha_id,
                "p_total_esperado": dados.total_esperado,
            },
            {
                "campanha_nao_encontrada": "Campanha não encontrada.",
                "dados_alterados": (
                    "A lista mudou depois do backup. Baixe uma nova planilha e tente novamente."
                ),
            },
        )
        quantidade = resultado.get("quantidade_excluida")
        if not isinstance(quantidade, int) or quantidade < 0:
            logger.error(
                "RPC limpar_participantes_admin devolveu quantidade inválida"
            )
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)
        return {
            "resultado": "sucesso",
            "quantidade_excluida": quantidade,
            "mensagem": f"{quantidade} participante(s) excluída(s).",
        }

    @api.get("/admin/participantes.csv")
    def exportar_participantes_csv(
        request: Request, campanha_id: int
    ) -> Response:
        exigir_admin(request)
        resposta = executar_consulta(
            lambda: database.rpc(
                "listar_participantes_admin", {"p_campanha_id": campanha_id}
            ).execute()
        )
        linhas = getattr(resposta, "data", None)
        if not isinstance(linhas, list):
            logger.error("RPC listar_participantes_admin não devolveu uma lista")
            raise HTTPException(status_code=503, detail=MENSAGEM_BANCO_INDISPONIVEL)

        arquivo = io.StringIO(newline="")
        escritor = csv.writer(arquivo, delimiter=";")
        escritor.writerow(
            [
                "Código",
                "Nome",
                "WhatsApp",
                "Prêmio",
                "Data de nascimento",
                "Autorização de aniversário em",
                "Autorização de aniversário revogada em",
                "Versão da política",
                "Ciência do aviso de privacidade em",
                "Participação",
                "Resgatado em",
                "Observação",
            ]
        )
        for linha in linhas:
            escritor.writerow(
                [
                    proteger_celula_csv(linha.get("codigo_voucher")),
                    proteger_celula_csv(linha.get("nome")),
                    proteger_celula_csv(linha.get("whatsapp")),
                    proteger_celula_csv(linha.get("premio")),
                    proteger_celula_csv(linha.get("data_nascimento")),
                    proteger_celula_csv(
                        linha.get("consentimento_aniversario_em")
                    ),
                    proteger_celula_csv(
                        linha.get("consentimento_aniversario_revogado_em")
                    ),
                    proteger_celula_csv(
                        linha.get("politica_privacidade_versao")
                    ),
                    proteger_celula_csv(linha.get("ciencia_privacidade_em")),
                    proteger_celula_csv(linha.get("data_participacao")),
                    proteger_celula_csv(linha.get("resgatado_em")),
                    proteger_celula_csv(linha.get("observacao_resgate")),
                ]
            )

        conteudo = arquivo.getvalue().encode("utf-8-sig")
        return Response(
            content=conteudo,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="participantes-campanha-{campanha_id}.csv"'
                ),
                "X-Total-Registros": str(len(linhas)),
            },
        )

    return api


app = create_app()
