import unicodedata
from datetime import date
from logging import Logger
from core.improved_context_classes import EnhancedUserContext
from core.agents.base_agent import BaseBedrockAgent  # Import the base class

KEYWORDS_USER_CONTEXT = [
    "mis",
    "mi",
    "yo",
    "voy",
    "hice",
    "hago",
    "personales",
    "personal" "objetivos",
]


from core.helpers.helpers import get_agent_config


class RagAgent(BaseBedrockAgent):
    def __init__(
        self,
        logger: Logger,
        user_context: EnhancedUserContext,
        boto3_client,
        agent_responses_feedback,
        config,
        session_id,
        session_local,
        source,
    ):
        rag_agent_config = get_agent_config(user_context.user_email, agent_type="rag")
        super().__init__(
            logger,
            user_context,
            boto3_client,
            agent_responses_feedback,
            config,
            session_id,
            session_local,
            source,
            rag_agent_config["agent_id"],
            rag_agent_config["agent_alias_id"],
        )

    def execute_rag_agent(self, question: str):
        last_message = self.clean_input_text(self.normalize_text(question))

        self.logger.info(f"[Bedrock RAG] Mensaje final enviado {last_message}")

        keywords_user = [
            kw for kw in last_message.split() if kw.lower() in KEYWORDS_USER_CONTEXT
        ]

        if len(keywords_user) >= 1:
            last_message += f"""
                El usuario hizo un pregunta personal referente a el.
                Para armar la respuesta y la query, tene en cuenta su nombre que es {self.user_context.name}.
                En la query deberas utilizar los campos 'nom_pro_cli' o si este no se encuentra usar 'nom_usu' para filtrar.
            """

        if self.user_context.nickname is not None:
            last_message += f"""
                Utiliza el apodo del usuario para responder, el cual es {self.user_context.nickname}.
                Tene en cuenta para algunas preguntas sobre el dia o fecha actual que hoy es {date.today().isoformat()}
            """

        return self._invoke_bedrock_agent(last_message)
