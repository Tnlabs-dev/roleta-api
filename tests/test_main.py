import os
from collections import deque
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("SENHA_FUNCIONARIA", "test-password")
os.environ.setdefault("SENHA_ADMIN", "admin-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "a" * 64)

import main  # noqa: E402


@dataclass
class FakeResponse:
    data: list[dict[str, Any]]


class FakeQuery:
    def __init__(self, database: "FakeDatabase", source: str) -> None:
        self.database = database
        self.call: dict[str, Any] = {"source": source, "filters": []}

    def select(self, columns: str) -> "FakeQuery":
        self.call.update(action="select", columns=columns)
        return self

    def insert(self, payload: dict[str, Any]) -> "FakeQuery":
        self.call.update(action="insert", payload=payload)
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self.call["filters"].append(("eq", column, value))
        return self

    def order(self, column: str) -> "FakeQuery":
        self.call["order"] = column
        return self

    def limit(self, value: int) -> "FakeQuery":
        self.call["limit"] = value
        return self

    def execute(self) -> FakeResponse:
        self.database.calls.append(self.call)
        if not self.database.responses:
            raise AssertionError("O teste não preparou uma resposta para esta consulta.")
        response = self.database.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)


class FakeDatabase:
    def __init__(self, *responses: list[dict[str, Any]] | Exception) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, f"table:{name}")

    def rpc(self, name: str, params: dict[str, Any]) -> FakeQuery:
        query = FakeQuery(self, f"rpc:{name}")
        query.call.update(action="rpc", params=params)
        return query


@pytest.fixture
def settings() -> main.Settings:
    return main.Settings(
        supabase_url="https://example.supabase.co",
        supabase_key="test-key",
        senha_funcionaria="test-password",
        senha_admin="admin-test-password",
        admin_session_secret="a" * 64,
    )


def client_for(settings: main.Settings, database: FakeDatabase) -> TestClient:
    return TestClient(main.create_app(settings=settings, database=database))


def test_ping_nao_consulta_banco(settings: main.Settings) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).get("/ping")

    assert response.status_code == 200
    assert response.json() == {"status": "acordado"}
    assert database.calls == []


def test_cors_aceita_apenas_origem_oficial(settings: main.Settings) -> None:
    client = client_for(settings, FakeDatabase())
    response = client.options(
        "/sortear/abcdefgh",
        headers={
            "Origin": main.ORIGEM_OFICIAL,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == main.ORIGEM_OFICIAL
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers

    delete_preflight = client.options(
        "/admin/participantes?campanha_id=1",
        headers={
            "Origin": main.ORIGEM_OFICIAL,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert delete_preflight.status_code == 200
    assert "DELETE" in delete_preflight.headers["access-control-allow-methods"]

    blocked = client.options(
        "/sortear/abcdefgh",
        headers={
            "Origin": "https://site-nao-autorizado.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in blocked.headers


def test_gerar_convite_rejeita_senha_incorreta_sem_consultar_banco(
    settings: main.Settings,
) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).post(
        "/gerar-convite", json={"senha": "errada"}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Senha incorreta."}
    assert database.calls == []


def test_gerar_convite_cria_codigo_curto_e_token_interno(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.secrets, "randbelow", lambda limite: 482_731)
    monkeypatch.setattr(main.secrets, "token_urlsafe", lambda tamanho: "A" * 32)
    database = FakeDatabase(
        [
            {
                "resultado": "sucesso",
                "codigo_gerado": "482731",
                "campanha": "Campanha de testes",
                "expira_em": "2026-08-14T16:30:00+00:00",
            }
        ]
    )
    response = client_for(settings, database).post(
        "/gerar-convite", json={"senha": "test-password"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "codigo": "482731",
        "token": "482731",
        "campanha": "Campanha de testes",
        "expira_em": "2026-08-14T16:30:00+00:00",
        "validade_minutos": 30,
    }
    assert database.calls[0]["source"] == "rpc:gerar_convite_roleta"
    params = database.calls[0]["params"]
    assert params["p_codigo_curto"] == "482731"
    assert main.TOKEN_LEGADO_PATTERN.fullmatch(params["p_token"])
    assert len(params["p_token"]) == 32
    assert params["p_expira_em"].endswith("+00:00")


def test_gerar_convite_tenta_outro_codigo_quando_ha_colisao(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    codigos = iter([123_456, 654_321])
    monkeypatch.setattr(main.secrets, "randbelow", lambda limite: next(codigos))
    monkeypatch.setattr(main.secrets, "token_urlsafe", lambda tamanho: "B" * 32)
    database = FakeDatabase(
        [{"resultado": "codigo_em_uso"}],
        [
            {
                "resultado": "sucesso",
                "codigo_gerado": "654321",
                "campanha": "Campanha de testes",
                "expira_em": "2026-08-14T16:30:00+00:00",
            }
        ],
    )

    response = client_for(settings, database).post(
        "/gerar-convite", json={"senha": "test-password"}
    )

    assert response.status_code == 200
    assert response.json()["codigo"] == "654321"
    assert [call["params"]["p_codigo_curto"] for call in database.calls] == [
        "123456",
        "654321",
    ]


def test_gerar_convite_exige_campanha_ativa(settings: main.Settings) -> None:
    database = FakeDatabase([{"resultado": "sem_campanha"}])
    response = client_for(settings, database).post(
        "/gerar-convite", json={"senha": "test-password"}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Não existe uma campanha ativa."}


def test_limite_de_senha_e_separado_por_rota(settings: main.Settings) -> None:
    database = FakeDatabase([{"valido": True, "mensagem": None, "campanha": "Teste"}])
    client = client_for(settings, database)

    for _ in range(5):
        assert client.post("/gerar-convite", json={"senha": "errada"}).status_code == 401

    blocked = client.post("/gerar-convite", json={"senha": "errada"})
    assert blocked.status_code == 429

    # As tentativas de senha não devem bloquear a conferência de um convite.
    verificar = client.get("/verificar-token/abcdefgh")
    assert verificar.status_code == 200
    assert verificar.json() == {"valido": True, "campanha": "Teste"}


def test_verificar_codigo_invalido_nao_consulta_banco(settings: main.Settings) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).get("/verificar-codigo/12ab")

    assert response.status_code == 404
    assert response.json() == {"detail": "Código não existe!"}
    assert database.calls == []


@pytest.mark.parametrize(
    ("database_result", "expected"),
    [
        (
            [{"valido": False, "mensagem": "Código não existe!", "campanha": None}],
            {"valido": False, "mensagem": "Código não existe!"},
        ),
        (
            [
                {
                    "valido": False,
                    "mensagem": "Este código já foi utilizado!",
                    "campanha": "Teste",
                }
            ],
            {
                "valido": False,
                "mensagem": "Este código já foi utilizado!",
                "campanha": "Teste",
            },
        ),
        (
            [{"valido": True, "mensagem": None, "campanha": "Teste"}],
            {"valido": True, "campanha": "Teste"},
        ),
    ],
)
def test_verificar_codigo(
    settings: main.Settings,
    database_result: list[dict[str, Any]],
    expected: dict[str, Any],
) -> None:
    database = FakeDatabase(database_result)
    response = client_for(settings, database).get("/verificar-codigo/482731")

    assert response.status_code == 200
    assert response.json() == expected
    assert database.calls[0] == {
        "source": "rpc:verificar_token_roleta",
        "filters": [],
        "action": "rpc",
        "params": {"p_token": "482731"},
    }


def test_verificar_token_legado_continua_compativel(settings: main.Settings) -> None:
    database = FakeDatabase(
        [{"valido": True, "mensagem": None, "campanha": "Teste"}]
    )

    response = client_for(settings, database).get("/verificar-token/abcdefgh")

    assert response.status_code == 200
    assert response.json() == {"valido": True, "campanha": "Teste"}


def test_premios_nao_expoe_estoque_nem_probabilidade(settings: main.Settings) -> None:
    database = FakeDatabase(
        [
            {
                "resultado": "sucesso",
                "mensagem": None,
                "campanha_id": 1,
                "campanha_nome": "Campanha de testes",
                "texto_consentimento": "Li o Aviso de Privacidade da campanha.",
                "politica_privacidade_versao": "1.0",
                "premio_id": 7,
                "premio_nome": "Brinde",
                "posicao_roleta": 2,
            }
        ]
    )
    response = client_for(settings, database).get("/premios")

    assert response.status_code == 200
    assert response.json() == {
        "status": "sucesso",
        "campanha": {
            "id": 1,
            "nome": "Campanha de testes",
            "texto_consentimento": "Li o Aviso de Privacidade da campanha.",
            "politica_privacidade_versao": "1.0",
        },
        "dados": [{"id": 7, "nome": "Brinde", "posicao_roleta": 2}],
    }
    assert "estoque_disponivel" not in response.text
    assert "peso_sorteio" not in response.text
    assert database.calls[0]["source"] == "rpc:listar_premios_roleta"


def test_premios_exige_campanha_ativa(settings: main.Settings) -> None:
    database = FakeDatabase([{"resultado": "sem_campanha"}])
    response = client_for(settings, database).get("/premios")

    assert response.status_code == 409
    assert response.json() == {"detail": "Não existe uma campanha ativa."}


def test_sortear_normaliza_dados_e_usa_uma_unica_rpc(settings: main.Settings) -> None:
    database = FakeDatabase(
        [
            {
                "resultado": "sucesso",
                "mensagem": "Parabéns Ana Silva, você ganhou: Brinde!",
                "premio": "Brinde",
                "indice_roleta": 3,
                "participante_id": 42,
            }
        ]
    )
    response = client_for(settings, database).post(
        "/sortear/abcdefgh",
        json={
            "nome": "  Ana   Silva  ",
            "whatsapp": "(11) 99999-9999",
            "ciencia_privacidade": True,
            "data_nascimento": None,
            "consentimento_aniversario": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "sucesso",
        "mensagem": "Parabéns Ana Silva, você ganhou: Brinde!",
        "premio": "Brinde",
        "indice_roleta": 3,
        "codigo_voucher": "CPR-000042",
    }
    assert len(database.calls) == 1
    assert database.calls[0] == {
        "source": "rpc:sortear_premio_com_privacidade",
        "filters": [],
        "action": "rpc",
        "params": {
            "p_token": "abcdefgh",
            "p_nome": "Ana Silva",
            "p_whatsapp": "11999999999",
            "p_ciencia_privacidade": True,
            "p_data_nascimento": None,
            "p_consentimento_aniversario": False,
        },
    }


def test_personaliza_nome_da_api_e_prefixo_do_voucher(
    settings: main.Settings,
) -> None:
    configuracao_passo_firme = replace(
        settings,
        api_display_name="Passo Firme API",
        voucher_prefix="PFR",
    )
    database = FakeDatabase(
        [
            {
                "resultado": "sucesso",
                "mensagem": "Prêmio confirmado!",
                "premio": "Brinde",
                "indice_roleta": 1,
                "participante_id": 7,
            }
        ]
    )
    client = client_for(configuracao_passo_firme, database)

    response = client.post(
        "/sortear/abcdefgh",
        json={
            "nome": "Ana Silva",
            "whatsapp": "11999999999",
            "ciencia_privacidade": True,
            "data_nascimento": None,
            "consentimento_aniversario": False,
        },
    )

    assert client.app.title == "Passo Firme API"
    assert response.status_code == 200
    assert response.json()["codigo_voucher"] == "PFR-000007"


@pytest.mark.parametrize(
    ("codigo", "status_code", "detail"),
    [
        ("token_invalido", 404, "Código não existe!"),
        ("token_utilizado", 409, "Este código já foi utilizado!"),
        ("token_cancelado", 409, "Este código foi cancelado."),
        (
            "codigo_expirado",
            409,
            "Este código expirou. Peça um novo código no caixa.",
        ),
        ("campanha_inativa", 409, "Esta campanha não está ativa."),
        ("sem_premios", 409, "Acabaram os prêmios no estoque!"),
    ],
)
def test_sortear_traduz_resultados_conhecidos(
    settings: main.Settings, codigo: str, status_code: int, detail: str
) -> None:
    database = FakeDatabase([{"resultado": codigo}])
    response = client_for(settings, database).post(
        "/sortear/abcdefgh",
        json={
            "nome": "Ana",
            "whatsapp": "11999999999",
            "ciencia_privacidade": True,
        },
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_validacao_impede_rpc_com_dados_invalidos(settings: main.Settings) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).post(
        "/sortear/abcdefgh",
        json={"nome": "A", "whatsapp": "123", "ciencia_privacidade": True},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Digite um nome válido."}
    assert database.calls == []


def test_validacao_exige_ciencia_do_aviso_de_privacidade(
    settings: main.Settings,
) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).post(
        "/sortear/abcdefgh",
        json={
            "nome": "Ana",
            "whatsapp": "11999999999",
            "ciencia_privacidade": False,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Você precisa confirmar que leu o Aviso de Privacidade."
    }
    assert database.calls == []


def test_aniversario_opcional_pode_ser_autorizado_por_adulta(
    settings: main.Settings,
) -> None:
    database = FakeDatabase(
        [
            {
                "resultado": "sucesso",
                "mensagem": "Parabéns Ana, você ganhou: Brinde!",
                "premio": "Brinde",
                "indice_roleta": 1,
                "participante_id": 7,
            }
        ]
    )
    response = client_for(settings, database).post(
        "/sortear/abcdefgh",
        json={
            "nome": "Ana",
            "whatsapp": "11999999999",
            "ciencia_privacidade": True,
            "data_nascimento": "1990-05-20",
            "consentimento_aniversario": True,
        },
    )

    assert response.status_code == 200
    assert database.calls[0]["params"]["p_data_nascimento"] == "1990-05-20"
    assert database.calls[0]["params"]["p_consentimento_aniversario"] is True


@pytest.mark.parametrize(
    ("data_nascimento", "consentimento_aniversario", "detail"),
    [
        (
            None,
            True,
            "Informe a data de nascimento para autorizar a mensagem de aniversário.",
        ),
        (
            "1990-05-20",
            False,
            "Marque a autorização de aniversário para enviar a data de nascimento.",
        ),
        (
            "2020-05-20",
            True,
            "O cadastro de aniversário está disponível apenas para maiores de 18 anos.",
        ),
    ],
)
def test_validacao_impede_aniversario_sem_autorizacao_valida(
    settings: main.Settings,
    data_nascimento: str | None,
    consentimento_aniversario: bool,
    detail: str,
) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).post(
        "/sortear/abcdefgh",
        json={
            "nome": "Ana",
            "whatsapp": "11999999999",
            "ciencia_privacidade": True,
            "data_nascimento": data_nascimento,
            "consentimento_aniversario": consentimento_aniversario,
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert database.calls == []


def test_falha_do_banco_retorna_erro_generico(settings: main.Settings) -> None:
    database = FakeDatabase(RuntimeError("segredo interno do banco"))
    response = client_for(settings, database).get("/premios")

    assert response.status_code == 503
    assert response.json() == {"detail": main.MENSAGEM_BANCO_INDISPONIVEL}
    assert "segredo interno" not in response.text


def test_configuracao_exige_todas_as_variaveis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_KEY"):
        main.Settings.from_env()


def test_configuracao_valida_prefixo_do_voucher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCHER_PREFIX", "prefixo-invalido")

    with pytest.raises(RuntimeError, match="VOUCHER_PREFIX"):
        main.Settings.from_env()


def test_configuracao_le_identidade_da_loja(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_DISPLAY_NAME", "Passo Firme API")
    monkeypatch.setenv("VOUCHER_PREFIX", "pfr")
    monkeypatch.setenv("CODIGO_VALIDADE_MINUTOS", "60")

    settings = main.Settings.from_env()

    assert settings.api_display_name == "Passo Firme API"
    assert settings.voucher_prefix == "PFR"
    assert settings.codigo_validade_minutos == 60


@pytest.mark.parametrize("valor", ["quarenta", "4", "121"])
def test_configuracao_rejeita_validade_invalida(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("CODIGO_VALIDADE_MINUTOS", valor)

    with pytest.raises(RuntimeError, match="CODIGO_VALIDADE_MINUTOS"):
        main.Settings.from_env()


def cabecalho_admin(settings: main.Settings) -> dict[str, str]:
    token, _ = main.criar_sessao_admin(settings, agora=1_800_000_000)
    return {"Authorization": f"Bearer {token}"}


def test_login_admin_cria_sessao_sem_consultar_banco(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_000)
    database = FakeDatabase()
    response = client_for(settings, database).post(
        "/admin/login", json={"senha": "admin-test-password"}
    )

    assert response.status_code == 200
    corpo = response.json()
    assert corpo["duracao_segundos"] == settings.admin_session_seconds
    assert main.validar_sessao_admin(
        corpo["token"], settings, agora=1_800_000_001
    )
    assert database.calls == []
    assert response.headers["cache-control"] == "no-store"


def test_login_admin_rejeita_senha_incorreta(settings: main.Settings) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).post(
        "/admin/login", json={"senha": "senha-errada"}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Senha administrativa incorreta."}
    assert database.calls == []


def test_sessao_admin_rejeita_token_adulterado(settings: main.Settings) -> None:
    token, _ = main.criar_sessao_admin(settings)
    token_adulterado = token[:-1] + ("A" if token[-1] != "A" else "B")
    response = client_for(settings, FakeDatabase()).get(
        "/admin/sessao",
        headers={"Authorization": f"Bearer {token_adulterado}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": main.MENSAGEM_SESSAO_INVALIDA}


def test_sessao_admin_rejeita_token_malformado_sem_erro_interno(
    settings: main.Settings,
) -> None:
    response = client_for(settings, FakeDatabase()).get(
        "/admin/sessao",
        headers={"Authorization": "Bearer %%%.%%%"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": main.MENSAGEM_SESSAO_INVALIDA}


def test_painel_admin_exige_autenticacao_sem_consultar_banco(
    settings: main.Settings,
) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).get("/admin/painel")

    assert response.status_code == 401
    assert database.calls == []


def test_painel_admin_retorna_agregados_protegidos(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_001)
    painel = {
        "resultado": "sucesso",
        "campanha": {"id": 1, "nome": "Campanha Cappri 2026.2"},
        "metricas": {"participantes": 3},
        "premios": [],
        "participantes": [],
        "convites": [],
        "auditoria": [],
    }
    resumo_convites = {
        "metricas": {"convites_pendentes": 1, "convites_expirados": 2},
        "convites": [
            {
                "id": 9,
                "codigo": "482731",
                "token": "482731",
                "status": "pendente",
            }
        ],
    }
    database = FakeDatabase(
        [{"painel": painel}],
        [{"resumo": resumo_convites}],
    )
    response = client_for(settings, database).get(
        "/admin/painel?campanha_id=1", headers=cabecalho_admin(settings)
    )

    assert response.status_code == 200
    assert response.json()["convites"] == resumo_convites["convites"]
    assert response.json()["metricas"] == {
        "participantes": 3,
        "convites_pendentes": 1,
        "convites_expirados": 2,
    }
    assert database.calls[0] == {
        "source": "rpc:obter_painel_admin",
        "filters": [],
        "action": "rpc",
        "params": {"p_campanha_id": 1},
    }
    assert database.calls[1] == {
        "source": "rpc:obter_convites_admin",
        "filters": [],
        "action": "rpc",
        "params": {"p_campanha_id": 1},
    }


def test_admin_atualiza_premio_por_rpc_atomica(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_001)
    database = FakeDatabase([{"resultado": "sucesso", "premio_id": 7}])
    response = client_for(settings, database).put(
        "/admin/premios/7",
        headers=cabecalho_admin(settings),
        json={
            "nome": "Brinde especial",
            "estoque_disponivel": 12,
            "peso_sorteio": "25.5",
            "ativo": True,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"resultado": "sucesso", "premio_id": 7}
    assert database.calls[0]["source"] == "rpc:atualizar_premio_admin"
    assert database.calls[0]["params"] == {
        "p_premio_id": 7,
        "p_nome": "Brinde especial",
        "p_estoque_disponivel": 12,
        "p_peso_sorteio": "25.5",
        "p_ativo": True,
    }


def test_admin_revoga_aniversario_por_rpc_protegida(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_001)
    database = FakeDatabase([{"resultado": "sucesso", "participante_id": 9}])
    response = client_for(settings, database).patch(
        "/admin/participantes/9/revogar-aniversario",
        headers=cabecalho_admin(settings),
    )

    assert response.status_code == 200
    assert response.json() == {"resultado": "sucesso", "participante_id": 9}
    assert database.calls[0] == {
        "source": "rpc:revogar_consentimento_aniversario_admin",
        "filters": [],
        "action": "rpc",
        "params": {"p_participante_id": 9},
    }


def test_exportacao_csv_protege_formula_e_usa_utf8(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_001)
    database = FakeDatabase(
        [
            {
                "codigo_voucher": "CPR-000001",
                "nome": "=HIPERLINK(\"site\")",
                "whatsapp": "11999999999",
                "premio": "Óculos",
                "data_nascimento": "1990-05-20",
                "consentimento_aniversario_em": "2026-08-05T12:00:00+00:00",
                "consentimento_aniversario_revogado_em": None,
                "politica_privacidade_versao": "1.0",
                "ciencia_privacidade_em": "2026-08-05T12:00:00+00:00",
                "data_participacao": "2026-08-05T12:00:00+00:00",
                "resgatado_em": None,
                "observacao_resgate": None,
            }
        ]
    )
    response = client_for(settings, database).get(
        "/admin/participantes.csv?campanha_id=1",
        headers=cabecalho_admin(settings),
    )

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "'=HIPERLINK" in response.content.decode("utf-8-sig")
    assert "Data de nascimento" in response.content.decode("utf-8-sig")
    assert "1990-05-20" in response.content.decode("utf-8-sig")
    assert "Versão da política" in response.content.decode("utf-8-sig")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-total-registros"] == "1"


def test_limpeza_de_participantes_exige_sessao_sem_consultar_banco(
    settings: main.Settings,
) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).request(
        "DELETE",
        "/admin/participantes?campanha_id=1",
        json={"confirmacao": "EXCLUIR", "total_esperado": 3},
    )

    assert response.status_code == 401
    assert database.calls == []


def test_limpeza_rejeita_confirmacao_incorreta_sem_consultar_banco(
    settings: main.Settings,
) -> None:
    database = FakeDatabase()
    response = client_for(settings, database).request(
        "DELETE",
        "/admin/participantes?campanha_id=1",
        headers=cabecalho_admin(settings),
        json={"confirmacao": "excluir", "total_esperado": 3},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Digite EXCLUIR exatamente como solicitado."
    }
    assert database.calls == []


def test_limpeza_exclui_somente_quando_total_confere(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_001)
    database = FakeDatabase(
        [{"resultado": "sucesso", "quantidade_excluida": 3}]
    )
    response = client_for(settings, database).request(
        "DELETE",
        "/admin/participantes?campanha_id=7",
        headers=cabecalho_admin(settings),
        json={"confirmacao": "EXCLUIR", "total_esperado": 3},
    )

    assert response.status_code == 200
    assert response.json() == {
        "resultado": "sucesso",
        "quantidade_excluida": 3,
        "mensagem": "3 participante(s) excluída(s).",
    }
    assert database.calls[0] == {
        "source": "rpc:limpar_participantes_admin",
        "filters": [],
        "action": "rpc",
        "params": {"p_campanha_id": 7, "p_total_esperado": 3},
    }


def test_limpeza_interrompe_se_lista_mudou_depois_do_backup(
    settings: main.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_001)
    database = FakeDatabase(
        [{"resultado": "dados_alterados", "quantidade_excluida": 4}]
    )
    response = client_for(settings, database).request(
        "DELETE",
        "/admin/participantes?campanha_id=7",
        headers=cabecalho_admin(settings),
        json={"confirmacao": "EXCLUIR", "total_esperado": 3},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "A lista mudou depois do backup. Baixe uma nova planilha e tente novamente."
    }
