from app.models.cliente import Cliente, DoCliente
from app.models.campanhas import Campanha, CampanhaFollowup, CampanhaMensagem, CampanhaModo, CampanhaStatus, Envio, EnvioStatus, EnvioTipo
from app.models.config import Configuracao
from app.models.empresas import Empresa
from app.models.mensagens import Autor, Direcao, Mensagem
from app.models.roteiros import ETAPAS, Etapa, Porte, Roteiro, Segmento
from app.models.agenda import Reuniao, ReuniaoStatus, Vendedor, VendedorBloqueio, VendedorHorario
from app.models.leads import Blacklist, Busca, BuscaStatus, Lead, LeadStatus, PlanoBusca
from app.models.user import User
from app.models.webhook_event import WebhookEvent
from app.models.whatsapp import EvolutionServer, NumeroStatus, WhatsappNumber

__all__ = [
    "ETAPAS",
    "Autor",
    "Direcao",
    "Etapa",
    "Mensagem",
    "Porte",
    "Roteiro",
    "Segmento",
    "Blacklist",
    "Campanha",
    "Cliente",
    "DoCliente",
    "CampanhaMensagem",
    "CampanhaModo",
    "CampanhaStatus",
    "Configuracao",
    "Empresa",
    "Envio",
    "EnvioStatus",
    "EnvioTipo",
    "Busca",
    "BuscaStatus",
    "EvolutionServer",
    "Lead",
    "LeadStatus",
    "NumeroStatus",
    "PlanoBusca",
    "Reuniao",
    "ReuniaoStatus",
    "User",
    "Vendedor",
    "VendedorBloqueio",
    "VendedorHorario",
    "WebhookEvent",
    "WhatsappNumber",
]

import app.tenancy  # noqa: E402,F401  (filtro e carimbo por cliente)
