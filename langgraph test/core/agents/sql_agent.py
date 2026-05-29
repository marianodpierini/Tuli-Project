import unicodedata
from datetime import date
from logging import Logger
from core.improved_context_classes import EnhancedUserContext
from core.helpers.helpers import (
    valite_existing_response,
    is_context_independent_heuristic,
    classify_with_bedrock,
    get_agent_id,
)
from core.database.models import SuggestedQuestions

STOP_WORDS = {
    "en",
    "la",
    "el",
    "los",
    "las",
    "que",
    "de",
    "a",
    "y",
    "o",
    "un",
    "una",
    "?",
}


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


from core.agents.base_agent import BaseBedrockAgent
from core.helpers.helpers import (
    get_agent_config,
    titan_embed,
) 


class SqlAgent(BaseBedrockAgent):
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
        sql_agent_config = get_agent_config(user_context.user_email, agent_type="sql")
        super().__init__(
            logger,
            user_context,
            boto3_client,
            agent_responses_feedback,
            config,
            session_id,
            session_local,
            source,
            sql_agent_config["agent_id"],
            sql_agent_config["agent_alias_id"],
        )

    def execute_sql_agent(self, question: str):
        last_message = self.clean_input_text(self.normalize_text(question))

        self.logger.info(f"[Bedrock SQL] Mensaje final enviado {last_message}")

        keywords_user = [
            kw for kw in last_message.split() if kw.lower() in KEYWORDS_USER_CONTEXT
        ]

        keywords_cache = [
            kw.lower() for kw in last_message.split() if kw.lower() not in STOP_WORDS
        ]

        validation = valite_existing_response(
            self.session_id, keywords_cache, last_message, self.config
        )
        if validation:
            if validation != "Pregunta sin query":
                return validation
        else:
            save_question = False

            decision = is_context_independent_heuristic(last_message)

            if decision is True:
                save_question = True
            elif decision is None:
                response, total_tokens_llm = classify_with_bedrock(last_message)
                self.user_context.update_use_tokens(
                    self.source, total_tokens=total_tokens_llm
                )
                if response:
                    save_question = True

            new_q_id = None

            if save_question:
                with self.session_local() as session:
                    embedding = titan_embed(last_message, keywords_cache, self.config)
                    new_q = SuggestedQuestions(
                        nombre=last_message,
                        activa=True,
                        keywords=keywords_cache,
                        embedding=embedding,
                    )
                    session.add(new_q)
                    session.commit()

                    new_q_id = new_q.id

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

        return self._invoke_bedrock_agent(last_message, new_q_id)
