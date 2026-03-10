import unicodedata
import botocore
from datetime import date, datetime, timedelta, timezone
from logging import Logger
from core.improved_context_classes import EnhancedUserContext
from core.helpers.helpers import valite_existing_response, is_context_independent_heuristic, classify_with_bedrock, get_agent_id, titan_embed
from core.database.models import SuggestedQuestions

STOP_WORDS = {"en", "la", "el", "los", "las", "que", "de", "a", "y", "o", "un", "una", "?"}


KEYWORDS_USER_CONTEXT = [
        "mis", "mi", "yo", "voy", "hice", "hago", "personales", "personal"
        "objetivos",
    ]

class SqlAgent:
    def __init__(self, logger: Logger, user_context: EnhancedUserContext, boto3_client, agent_responses_feedback, config, session_id, session_local, source):
        self.logger = logger
        self.user_context = user_context
        self.boto3_client = boto3_client
        self.agent_responses_feedback = agent_responses_feedback
        self.config = config
        self.session_id = session_id
        self.session_local = session_local
        self.source = source

    def clean_input_text(self, text):
        text = ''.join(filter(str.isprintable, text))
        return text

    def normalize_text(self, text):
        return unicodedata.normalize('NFKC', text)

    def execute_sql_agent(self, question: str):
        last_message = self.clean_input_text(self.normalize_text(question))

        self.logger.info(f"[Bedrock SQL] Mensaje final enviado {last_message}")

        keywords_user = [kw for kw in last_message.split() if kw.lower() in KEYWORDS_USER_CONTEXT]

        keywords_cache = [kw.lower() for kw in last_message.split() if kw.lower() not in STOP_WORDS]

        validation = valite_existing_response(self.session_id, keywords_cache, last_message, self.config)

        if validation:
                if validation == "Pregunta sin query":
                    self.logger.info("La pregunta no tiene una query asociada.")
                else:
                    assistant_response = ""
                    event_stream = validation.get('completion')
                    
                    if isinstance(event_stream, botocore.eventstream.EventStream):
                        for event in event_stream:
                            if 'chunk' in event:
                                chunk_data = event['chunk']['bytes'].decode('utf-8')
                                assistant_response += chunk_data

                    self.logger.info(f"[AGENT RESPONSE] Respuesta del agente: {assistant_response.strip()}")
                    return assistant_response.strip()
        else:
            save_question = False

            decision = is_context_independent_heuristic(last_message)

            if decision is True:
                save_question = True
            elif decision is None:
                response, total_tokens_llm = classify_with_bedrock(last_message)
                self.user_context.update_use_tokens(self.source, total_tokens=total_tokens_llm)
                if response:
                    save_question = True

            if save_question:
                with self.session_local() as session:
                    embedding = titan_embed(last_message, keywords_cache, self.config)
                    new_q = SuggestedQuestions(
                        nombre=last_message,
                        activa=True, 
                        keywords=keywords_cache,
                        embedding=embedding
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

        
        params = {
            'agentId': 'DRSOAFDOTR',
            'agentAliasId': "XKJTFFEMPC",
            'sessionId': self.session_id,
            'inputText': last_message,
            'enableTrace': True,
            'sessionState': {
                "sessionAttributes": {
                    "suggestion_id": str(new_q_id) if new_q_id else ""
                }
            }
        }

        try:
            self.logger.info(f"Enviando solicitud a Bedrock con parámetros: {params}")
            response = self.boto3_client.invoke_agent(**params)

            # 8. Procesar EventStream correctamente
            assistant_response = ""
            event_stream = response.get('completion')
            
            if isinstance(event_stream, botocore.eventstream.EventStream):
                total_ms = 0
                total_tokens = 0
                total_steps = 0
                for event in event_stream:
                    if 'chunk' in event:
                        try:
                            chunk_data = event['chunk']['bytes'].decode('utf-8')
                            assistant_response += chunk_data
                        except Exception as decode_error:
                            self.logger.error(f"Error decodificando chunk: {decode_error}")

                    if 'trace' in event:
                        trace_data = event['trace'].get('trace', {}).get('orchestrationTrace', {})
                        if 'modelInvocationOutput' in trace_data:
                            self.user_context.update_use_tokens(self.source, trace_data=trace_data)

                            total_ms += int(trace_data.get("modelInvocationOutput", {}).get("metadata", {}).get("totalTimeMs", 0))
                            usage = (
                                trace_data.get("modelInvocationOutput", {})
                                .get("metadata", {})
                                .get("usage", {})
                            )
                            total = usage.get("inputTokens", 0) + usage.get("outputTokens", 0)
                            total_tokens += total
                            total_steps += 1

            else:
                self.logger.error(f"Tipo inesperado en 'completion': {type(event_stream)}")

            self.logger.info(f"[AGENT SQL RESPONSE] Respuesta del agente: {assistant_response.strip()}")
            self.agent_responses_feedback.put_item(Item={
                "id_thread": self.event.body["space_name"],
                "last_update_time": datetime.now().isoformat(),
                "bot_response_text": assistant_response.strip(),
                "user_question_text": last_message,
                "user": self.event.body["email"],
                "agend_id": "XKJTFFEMPC",
                "channel": self.source,
                "created_at": date.today().isoformat(),
                "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
            })
            return assistant_response.strip()
        
        except Exception as e:
            self.logger.error(f"Timeout al invocar agente Bedrock: {str(e)}")