import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional

try:
    from agno.knowledge.chunking.strategy import ChunkingStrategyType
    from agno.knowledge.document.base import Document
    from agno.knowledge.reader.base import Reader
    from agno.knowledge.types import ContentType
except ImportError:
    raise ImportError("Please install agno to use WebReaderKnowledge (pip install agno)")

from web_reader import read_url

@dataclass
class WebReaderKnowledge(Reader):
    """
    Agno Knowledge Reader that leverages the async `web-reader` library.
    It can read URLs and auto-detect if they are Youtube, RSS, Substacks, Reddit, etc.
    """

    @classmethod
    def get_supported_chunking_strategies(cls) -> List[ChunkingStrategyType]:
        return [
            ChunkingStrategyType.FIXED_SIZE_CHUNKER,
            ChunkingStrategyType.SEMANTIC_CHUNKER,
            ChunkingStrategyType.DOCUMENT_CHUNKER,
            ChunkingStrategyType.RECURSIVE_CHUNKER,
        ]

    @classmethod
    def get_supported_content_types(cls) -> List[ContentType]:
        return [ContentType.URL]

    def read(self, obj: Any, name: Optional[str] = None, password: Optional[str] = None) -> List[Document]:
        """
        Synchronous read wrapper.
        """
        url = str(obj)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, self._read_async_internal(url, name)).result()
                return result
        else:
            return loop.run_until_complete(self._read_async_internal(url, name))

    async def async_read(self, obj: Any, name: Optional[str] = None, password: Optional[str] = None) -> List[Document]:
        """
        Asynchronous read using `web_reader.read_url`.
        """
        url = str(obj)
        return await self._read_async_internal(url, name)

    async def _read_async_internal(self, url: str, name: Optional[str] = None) -> List[Document]:
        result = await read_url(url)
        if not result.success:
            return []
        
        doc = Document(
            name=name or result.title or url,
            id=url,
            meta_data=result.raw or {},
            content=result.text or "",
        )
        
        if self.chunk:
            return await self.achunk_document(doc)
        return [doc]
