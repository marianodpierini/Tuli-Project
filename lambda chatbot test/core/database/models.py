from sqlalchemy import (
    Column,
    Float,
    BigInteger,
    Boolean,
    Text,
    DateTime,
    String,
    Integer,
    func,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from pgvector.sqlalchemy import Vector

Base = declarative_base()


class ServiciosTcktsRvas(Base):
    __tablename__ = "servicios_tckts_rvas"
    __table_args__ = {"schema": "aptour"}

    @declared_attr
    def __mapper_args__(cls):
        return {"primary_key": [cls._rowid]}

    _rowid = Column(Integer, primary_key=True, autoincrement=True)

    dif = Column(Float, nullable=True)
    costo = Column(Float, nullable=True)
    difus = Column(Float, nullable=True)
    venta = Column(Float, nullable=True)
    id_res = Column(BigInteger, nullable=True)
    moneda = Column(Text, nullable=True)
    numero = Column(BigInteger, nullable=True)
    cie_res = Column(Boolean, nullable=True)
    cliente = Column(Text, nullable=True)
    costous = Column(Float, nullable=True)
    fec_ape = Column(DateTime, nullable=True)
    nom_ope = Column(Text, nullable=True)
    nom_usu = Column(Text, nullable=True)
    nor_dev = Column(Text, nullable=True)
    num_pax = Column(BigInteger, nullable=True)
    ventaus = Column(Float, nullable=True)
    duracion = Column(BigInteger, nullable=True)
    nom_depto = Column(Text, nullable=True)
    gea_member = Column(BigInteger, nullable=True)
    base_origen = Column(Text, nullable=True)
    nom_pro_cli = Column(Text, nullable=True)
    fecha_cierre = Column(DateTime, nullable=True)
    teytu_member = Column(BigInteger, nullable=True)
    ultima_compra = Column(DateTime, nullable=True)
    cambioblue_ape = Column(Float, nullable=True)
    cotizacion_ape = Column(Float, nullable=True)
    cambioblue_cierre = Column(Float, nullable=True)
    cotizacion_aptour = Column(Float, nullable=True)
    cotizacion_cierre = Column(Float, nullable=True)
    destino_terrestre = Column(Text, nullable=True)
    fecha_alta_agencia = Column(DateTime, nullable=True)
    tour_vector_member = Column(BigInteger, nullable=True)
    ciudad_origen_agencia = Column(Text, nullable=True)
    emision_aerotour_color = Column(Text, nullable=True)
    alerta_churning_agencia = Column(Text, nullable=True)
    provincia_origen_agencia = Column(Text, nullable=True)
    segmentacion_agencia_base = Column(BigInteger, nullable=True)
    segmentacion_agencia_nueva = Column(Float, nullable=True)
    segmentacion_agencia_freelance = Column(BigInteger, nullable=True)
    segmentacion_agencia_potencial = Column(Float, nullable=True)
    promedio_dias_frecuencia_compra = Column(Float, nullable=True)
    segmentacion_agencia_precaucion = Column(BigInteger, nullable=True)
    segmentacion_agencia_estrategica = Column(Float, nullable=True)
    _airbyte_ab_id = Column(String, nullable=True)
    _airbyte_emitted_at = Column(DateTime(timezone=True), nullable=True)
    _airbyte_normalized_at = Column(DateTime(timezone=True), nullable=True)
    _airbyte_servicios_tckts_rvas_hashid = Column(Text, nullable=True)


class SuggestedQuestions(Base):
    __tablename__ = "suggested_questions"
    __table_args__ = {"schema": "aptour"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(Text, nullable=False)
    descripcion = Column(Text)
    categoria = Column(Text)
    sql_query = Column(Text)
    parametros = Column(JSONB)
    activa = Column(Boolean, default=True)
    prioridad = Column(Integer)
    keywords = Column(ARRAY(Text))
    embedding = Column(Vector(1024))
    frecuencia = Column(Text)
    template_respuesta = Column(Text)
    name_mappings = Column(JSONB)
    kb_document = Column(Text)
    needs_clarification = Column(Text)
    instrucciones_cortas = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class TipsOperadores(Base):
    __tablename__ = "tips_operadores"
    __table_args__ = {"schema": "turi"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    region = Column(Text, nullable=False)
    pais = Column(Text, nullable=False)
    destino = Column(Text, nullable=False)
    tipo_prestador = Column(Text)
    prestador = Column(Text)
    operador = Column(Text)
    servicios = Column(Text)
    tipo_servicio = Column(Text)
    categoria = Column(Text)
    moneda = Column(Text)
    canal_cotizacion = Column(Text)
    integracion_cotizador = Column(Text)
    manual = Column(Text)
    tarifario = Column(Text)
    web = Column(Text)
    especificaciones_comision = Column(Text)
    prioridad = Column(Text)
    comentarios = Column(Text)
    estado_carga = Column(Text)
    fecha_actualizacion = Column(DateTime, server_default=func.now(), onupdate=func.now())
    actualizado_por = Column(Text)
    activo = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class TableMetadata(Base):
    __tablename__ = "table_metadata"
    __table_args__ = {"schema": "catalog"}

    database_name = Column(Text, nullable=False, primary_key=True)
    schema_name = Column(Text, nullable=False, primary_key=True)
    table_name = Column(Text, nullable=False, primary_key=True)
    object_type = Column(Text)
    dominio = Column(Text)
    descripcion_tecnica_corta = Column(Text)
    descripcion_negocio = Column(Text)
    granularidad = Column(Text)
    default_time_column = Column(Text)
    frecuencia_actualizacion = Column(Text)
    fuente = Column(Text)
    owner_negocio = Column(Text)
    owner_tecnico = Column(Text)
    estado_metadata = Column(Text)
    nivel_confianza = Column(Text)
    usable_por_turi = Column(Boolean, default=False)
    visible_usuario = Column(Boolean, default=False)
    sensibilidad = Column(Text)
    notas = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    embedding = Column(Vector(1024))


class ColumnMetadata(Base):
    __tablename__ = "column_metadata"
    __table_args__ = {"schema": "catalog"}

    database_name = Column(Text, nullable=False, primary_key=True)
    schema_name = Column(Text, nullable=False, primary_key=True)
    table_name = Column(Text, nullable=False, primary_key=True)
    column_name = Column(Text, nullable=False, primary_key=True)
    ordinal_position = Column(Integer)
    tipo_dato = Column(Text)
    descripcion_tecnica = Column(Text)
    descripcion_negocio = Column(Text)
    valores_posibles = Column(Text)
    sinonimos_usuario = Column(Text)
    ejemplo_filtro = Column(Text)
    es_nullable = Column(Boolean)
    es_pk = Column(Boolean)
    es_fk = Column(Boolean)
    estado_metadata = Column(Text)
    nivel_confianza = Column(Text)
    usable_por_turi = Column(Boolean, default=False)
    visible_usuario = Column(Boolean, default=False)
    sensibilidad = Column(Text)
    notas = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    embedding = Column(Vector(1024))

class Glossary(Base):
    __tablename__ = "glossary"
    __table_args__ = {"schema": "catalog"}

    database_name = Column(Text, nullable=False, primary_key=True)
    dominio = Column(Text, nullable=False, primary_key=True)
    termino = Column(Text, nullable=False, primary_key=True)
    significado = Column(Text)
    mapeo_tecnico = Column(Text)
    tipo_mapeo = Column(Text)
    sinonimos = Column(Text)
    estado_metadata = Column(Text)
    usable_por_turi = Column(Boolean, default=False)
    notas = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    embedding = Column(Vector(1024))

class BusinessRules(Base):
    __tablename__ = "business_rules"
    __table_args__ = {"schema": "catalog"}

    database_name = Column(Text, nullable=False, primary_key=True)
    dominio = Column(Text, nullable=False, primary_key=True)
    concept_name = Column(Text, nullable=False, primary_key=True)
    definicion = Column(Text)
    rule_sql = Column(Text)
    base_metric = Column(Text)
    default_time_window = Column(Text)
    sinonimos = Column(Text)
    estado_metadata = Column(Text)
    nivel_confianza = Column(Text)
    notas = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    embedding = Column(Vector(1024))
    
