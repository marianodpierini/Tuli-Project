from sqlalchemy import Column, Float, BigInteger, Boolean, Text, DateTime, String, Integer, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

Base = declarative_base()

class ServiciosTcktsRvas(Base):
    __tablename__ = "servicios_tckts_rvas"
    __table_args__ = {"schema": "aptour"}

    @declared_attr
    def __mapper_args__(cls):
        return {
            'primary_key': [cls._rowid]
        }

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
    frecuencia = Column(Text)
    template_respuesta = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
