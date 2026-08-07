# semantic_search/__init__.py

from .embedder import (
    OpenRouterEmbedder,
    OrcaRouterEmbedder,
    LocalEmbedder,
    get_embedder,
    is_openrouter_available,
    is_orcarouter_available,
    is_local_embedding_configured,
    is_semantic_search_available,
)
from .vector_store import VectorStore
from .processor import DocumentProcessor

__all__ = [
    'OpenRouterEmbedder',
    'OrcaRouterEmbedder',
    'LocalEmbedder',
    'get_embedder',
    'is_openrouter_available',
    'is_orcarouter_available',
    'is_local_embedding_configured',
    'is_semantic_search_available',
    'VectorStore',
    'DocumentProcessor',
]
