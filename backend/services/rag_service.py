import logging
import time

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from services.warm_cache import get_embeddings

logger = logging.getLogger(__name__)


class RAGService:
    def __init__(self):
        self.vector_store = None
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            length_function=len,
        )

    def create_vector_store(self, resume_text: str, job_description: str):
        """Build FAISS index from resume + JD text. Uses cached embeddings."""
        t0 = time.perf_counter()
        embeddings = get_embeddings()

        combined = f"RESUME:\n{resume_text}\n\nJOB DESCRIPTION:\n{job_description}"
        chunks    = self.text_splitter.split_text(combined)
        documents = [Document(page_content=c) for c in chunks]

        logger.info(f"[RAG] Building FAISS index: {len(documents)} chunks…")
        self.vector_store = FAISS.from_documents(documents, embeddings)
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.info(f"[RAG] FAISS index built in {elapsed} ms")
        return self.vector_store

    def retrieve_context(self, query: str, k: int = 3):
        if self.vector_store is None:
            logger.warning("[RAG] Vector store not initialised")
            return []
        t0      = time.perf_counter()
        results = self.vector_store.similarity_search(query, k=k)
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.info(f"[RAG] retrieve_context k={k} → {len(results)} chunks in {elapsed} ms")
        return [doc.page_content for doc in results]

    def get_relevant_info(self, query: str) -> str:
        return "\n\n".join(self.retrieve_context(query, k=3))
