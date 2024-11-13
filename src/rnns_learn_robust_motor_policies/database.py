"""
Database tools for cataloguing trained models and notebook evaluations/figures.

Written with the help of Claude 3.5 Sonnet.
""" 

from datetime import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any
import hashlib
import json
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from jaxtyping import PyTree
from sqlalchemy import (
    Boolean,
    Column, 
    DateTime, 
    Integer, 
    Float, 
    ForeignKey,
    JSON, 
    String, 
    create_engine, 
    inspect,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, relationship, sessionmaker
from sqlalchemy.sql.type_api import TypeEngine

from feedbax import save

from rnns_learn_robust_motor_policies import DB_DIR, MODELS_DIR


MODELS_TABLE_NAME = 'models'
EVALUATIONS_TABLE_NAME = 'notebook_evaluations'
FIGURES_TABLE_NAME = 'figures'


logger = logging.getLogger(__name__)


Base = declarative_base()


class ModelRecord(Base):
    __tablename__ = MODELS_TABLE_NAME
    
    id = Column(Integer, primary_key=True)
    hash = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    notebook_id = Column(String, nullable=False)  # e.g. "1-1"
    
    # File paths
    model_path = Column(String)
    train_history_path = Column(String)
    replicate_info_path = Column(String)
    

MODEL_RECORD_BASE_ATTRS = ModelRecord.__table__.columns.keys()
    
    
class NotebookEvaluationRecord(Base):
    """Represents a single evaluation of a notebook."""
    __tablename__ = EVALUATIONS_TABLE_NAME
    
    id = Column(Integer, primary_key=True)
    hash = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Which notebook was evaluated
    eval_notebook_id = Column(String, nullable=False)  # e.g. "1-2a"
    
    # Reference to the model used
    model_id = Column(Integer, ForeignKey(f'{MODELS_TABLE_NAME}.id'))
    model = relationship("ModelRecord")  
    
    # Parameters used for evaluation (as JSON to handle different params per notebook)
    eval_parameters = Column(JSON)
    
    # Output directory containing rendered notebook
    output_dir = Column(String)
    
    # Relationship to figures
    figures = relationship("FigureRecord", back_populates="evaluation")


class FigureRecord(Base):
    """Represents a figure generated during notebook evaluation."""
    __tablename__ = FIGURES_TABLE_NAME
    
    id = Column(Integer, primary_key=True)
    hash = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Reference to the evaluation this figure belongs to
    evaluation_id = Column(Integer, ForeignKey(f'{EVALUATIONS_TABLE_NAME}.id'))
    evaluation = relationship("NotebookEvaluationRecord", back_populates="figures")
    
    # Figure metadata
    identifier = Column(String)  # e.g. "center_out_sets/all_evals_single_replicate"
    file_path = Column(String)


TABLE_NAME_TO_MODEL = {
    mapper.class_.__tablename__: mapper.class_
    for mapper in Base.registry.mappers
}


def get_sql_type(value) -> TypeEngine:
    if isinstance(value, bool):
        return Boolean()
    elif isinstance(value, int):
        return Integer()
    elif isinstance(value, float):
        return Float()
    elif isinstance(value, str):
        return String()
    else:
        return JSON()


def update_table_schema(engine, table_name: str, columns: Dict[str, Any]):
    """Dynamically add new columns using Alembic operations."""
    Base.metadata.create_all(engine)
    
    # Get existing columns
    inspector = inspect(engine)
    existing_columns = {col['name'] for col in inspector.get_columns(table_name)}
    
    # Create Alembic context
    context = MigrationContext.configure(engine.connect())
    op = Operations(context)
    model_class = TABLE_NAME_TO_MODEL[table_name]
    
    # Add only new columns using Alembic operations
    for key, value in columns.items():
        if key not in existing_columns:
            column = Column(key, get_sql_type(value), nullable=True)
            setattr(model_class, key, column)
            op.add_column(table_name, column)
            
    Base.metadata.clear()             # Clear SQLAlchemy's cached schema
    Base.metadata.create_all(engine)  # Recreate tables with new schema
            

def init_db(db_path: str = "sqlite:///models.db"):
    engine = create_engine(db_path)
    Base.metadata.create_all(engine)
    
    inspector = inspect(engine)
    
    for table_name in inspector.get_table_names():
        existing_columns = inspector.get_columns(table_name)
        
        model = TABLE_NAME_TO_MODEL[table_name]
        
        for col in existing_columns:
            try:
                getattr(model, col['name'])
            except AttributeError:
                setattr(
                    model, 
                    col['name'], 
                    Column(col['name'], col['type'], nullable=col['nullable']),
                ) 
    
    Base.metadata.clear()             # Clear SQLAlchemy's cached schema
    Base.metadata.create_all(engine)  # Recreate tables with new schema
    
    return sessionmaker(bind=engine)()


def get_db_session(key: str):
    """Create a database session for the project database with the given key."""
    return init_db(f"sqlite:///{DB_DIR}/{key}.db")


def hash_file(path: Path) -> str:
    """Generate MD5 hash of file."""
    md5 = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            md5.update(chunk)
    return md5.hexdigest()


def generate_temp_path(directory: Path, prefix: str = "temp_", suffix: str = ".eqx") -> Path:
    """Generate a temporary file path."""
    return directory / f"{prefix}{uuid.uuid4()}{suffix}"


def save_with_hash(tree: PyTree, directory: Path, hyperparameters: Optional[Dict] = None) -> tuple[str, Path]:
    """Save object to temporary file, compute hash, and move to final location."""
    temp_path = generate_temp_path(directory)
    save(temp_path, tree, hyperparameters=hyperparameters)
    
    file_hash = hash_file(temp_path)
    final_path = directory / f"{file_hash}.eqx"
    temp_path.rename(final_path)
    
    return file_hash, final_path


def query_model_records(
    session: Session,
    filters: Optional[Dict[str, Any]] = None,
    match_all: bool = True,
) -> list[ModelRecord]:
    """Query model records from database matching filter criteria.
    
    Args:
        session: Database session
        filters: Dictionary of {column: value} pairs to filter by
        match_all: If True, return only records matching all filters (AND).
                  If False, return records matching any filter (OR).
    
    Returns:
        List of matching ModelRecord records
    """
    if not filters:
        return session.query(ModelRecord).all()
        
    query = session.query(ModelRecord)
    
    conditions = [
        getattr(ModelRecord, key) == value 
        for key, value in filters.items()
    ]
    
    if match_all:
        for condition in conditions:
            query = query.filter(condition)
    else:
        from sqlalchemy import or_
        query = query.filter(or_(*conditions))
        
    return query.all()


def get_model_record(
    session: Session,
    **filters: Any
) -> Optional[ModelRecord]:
    """Get single model record matching all filters exactly.
    Raises ValueError if multiple matches found."""
    matches = query_model_records(session, filters)
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"Multiple models found that match filters: {filters}")
    return matches[0]


def save_model_and_add_record(
    session: Session,
    notebook_id: str,
    model: Any,
    model_hyperparameters: Dict[str, Any],
    other_hyperparameters: Dict[str, Any],
    train_history: Optional[Any] = None,
    train_history_hyperparameters: Optional[Dict[str, Any]] = None,
    replicate_info: Optional[Any] = None,
    replicate_info_hyperparameters: Optional[Dict[str, Any]] = None,
) -> ModelRecord:
    """Save model files with hash-based names and add database record."""
    
    # Save model and get hash-based filename
    model_hash, model_path = save_with_hash(model, MODELS_DIR, model_hyperparameters)
    
    hyperparameters = model_hyperparameters | other_hyperparameters
    
    update_table_schema(session.bind, MODELS_TABLE_NAME, hyperparameters)
    
    # Save associated files if provided
    train_history_path = None
    if train_history is not None:
        assert train_history_hyperparameters is not None, (
            "If saving training histories, must provide hyperparameters for deserialisation!"
        )
        # TODO: Could save with same hash as model, with suffix 
        _, train_history_path = save_with_hash(
            train_history, 
            MODELS_DIR,
            train_history_hyperparameters,
        )
        
    replicate_info_path = None
    if replicate_info is not None:
        assert replicate_info_hyperparameters is not None, (
            "If saving training histories, must provide hyperparameters for deserialisation!"
        )
        _, replicate_info_path = save_with_hash(
            replicate_info,
            MODELS_DIR,
            replicate_info_hyperparameters,
        )
    
    # Create database record
    model_record = ModelRecord(
        hash=model_hash,
        notebook_id=notebook_id,
        model_path=str(model_path),
        train_history_path=str(train_history_path) if train_history_path else None,
        replicate_info_path=str(replicate_info_path) if replicate_info_path else None,
        **hyperparameters,
    )
    
    # Delete existing record with same hash, if it exists
    existing_record = get_model_record(session, hash=model_hash)
    if existing_record is not None:
        session.delete(existing_record)
        session.commit()
        logger.info(f"Replacing existing database record for model with hash {model_hash}")
    
    session.add(model_record)
    session.commit()
    return model_record


def generate_eval_hash(model_id: Optional[int], eval_params: Dict[str, Any]) -> str:
    """Generate hash for notebook evaluation based on model ID and parameters."""
    eval_str = f"{model_id or 'None'}_{json.dumps(eval_params, sort_keys=True)}"
    return hashlib.md5(eval_str.encode()).hexdigest()


def add_notebook_evaluation(
    session: Session,
    model_id: Optional[int],  # Changed from ModelRecord to Optional[int]
    eval_notebook_id: str,
    eval_parameters: Dict[str, Any],
    output_base_dir: Path,
) -> NotebookEvaluationRecord:
    """Create new notebook evaluation record.
    
    Args:
        session: Database session
        model_id: ID of the model used (None for training notebooks)
        eval_notebook_id: ID of the notebook being evaluated
        eval_parameters: Parameters used for evaluation
        output_base_dir: Base directory for outputs
    """
    # Generate hash from model_id (if any) and parameters
    eval_hash = generate_eval_hash(
        model_id=model_id,
        eval_params=eval_parameters
    )
    
    output_dir = output_base_dir / eval_notebook_id / eval_hash
    
    eval_record = NotebookEvaluationRecord(
        hash=eval_hash,
        eval_notebook_id=eval_notebook_id,
        model_id=model_id,  # Can be None
        eval_parameters=eval_parameters,
        output_dir=str(output_dir),
    )
    
    session.add(eval_record)
    session.commit()
    return eval_record


def add_evaluation_figure(
    session: Session,
    evaluation: NotebookEvaluationRecord,
    figure: Any,
    identifier: str,
) -> FigureRecord:
    """Save figure and create database record."""
    # Save figure in subdirectory with same hash as evaluation
    figure_dir = Path(str(evaluation.output_dir)) / "figures"
    figure_dir.mkdir(exist_ok=True)
    
    # Generate unique filename for this figure
    figure_hash = hashlib.md5(f"{evaluation.hash}_{identifier}".encode()).hexdigest()
    figure_path = figure_dir / f"{figure_hash}.png"
    
    # Save figure
    figure.write_image(figure_path)  # For plotly
    # figure.savefig(figure_path)  # For matplotlib
    
    figure_record = FigureRecord(
        hash=figure_hash,
        evaluation_id=evaluation.id,
        identifier=identifier,
        file_path=str(figure_path),
    )
    
    session.add(figure_record)
    session.commit()
    return figure_record