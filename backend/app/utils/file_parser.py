"""
文件解析工具
支持PDF、Markdown、TXT文件的文本提取
"""

import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional


def _read_text_with_fallback(file_path: str) -> str:
    """
    讀取文本文件，UTF-8失敗時自動探測編碼。
    
    採用多級回退策略：
    1. 首先嚐試 UTF-8 解碼
    2. 使用 charset_normalizer 檢測編碼
    3. 回退到 chardet 檢測編碼
    4. 最終使用 UTF-8 + errors='replace' 兜底
    
    Args:
        file_path: 文件路徑
        
    Returns:
        解碼後的文本內容
    """
    data = Path(file_path).read_bytes()

    if data.lstrip().startswith(b'{\\rtf'):
        try:
            result = subprocess.run(
                ['textutil', '-convert', 'txt', '-stdout', file_path],
                check=True,
                capture_output=True,
                text=True
            )
            return result.stdout
        except Exception:
            return _strip_rtf_markup(data.decode('latin-1', errors='replace'))
    
    # 首先嚐試 UTF-8
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass
    
    # 嘗試使用 charset_normalizer 檢測編碼
    encoding = None
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best and best.encoding:
            encoding = best.encoding
    except Exception:
        pass
    
    # 回退到 chardet
    if not encoding:
        try:
            import chardet
            result = chardet.detect(data)
            encoding = result.get('encoding') if result else None
        except Exception:
            pass
    
    # 最終兜底：使用 UTF-8 + replace
    if not encoding:
        encoding = 'utf-8'
    
    return data.decode(encoding, errors='replace')


def _strip_rtf_markup(text: str) -> str:
    """Best-effort fallback for mislabeled RTF text files."""
    text = re.sub(r"\\'[0-9a-fA-F]{2}", lambda m: bytes.fromhex(m.group(0)[2:]).decode('cp1252', errors='replace'), text)
    text = re.sub(r'\\par[d]?', '\n', text)
    text = re.sub(r'\\[a-zA-Z]+-?\d* ?', '', text)
    text = re.sub(r'[{}]', '', text)
    return text.strip()


class FileParser:
    """文件解析器"""
    
    SUPPORTED_EXTENSIONS = {'.pdf', '.md', '.markdown', '.txt'}
    
    @classmethod
    def is_supported(cls, file_path: str) -> bool:
        """
        檢查文件是否為支持的格式
        
        Args:
            file_path: 文件路徑
            
        Returns:
            如果文件格式受支持則返回 True
        """
        suffix = Path(file_path).suffix.lower()
        return suffix in cls.SUPPORTED_EXTENSIONS
    
    @classmethod
    def extract_text(cls, file_path: str) -> str:
        """
        從文件中提取文本
        
        Args:
            file_path: 文件路徑
            
        Returns:
            提取的文本內容
        """
        path = Path(file_path)
        
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        suffix = path.suffix.lower()
        
        if suffix not in cls.SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件格式: {suffix}")
        
        if suffix == '.pdf':
            return cls._extract_from_pdf(file_path)
        elif suffix in {'.md', '.markdown'}:
            return cls._extract_from_md(file_path)
        elif suffix == '.txt':
            return cls._extract_from_txt(file_path)
        
        raise ValueError(f"無法處理的文件格式: {suffix}")
    
    @staticmethod
    def _extract_from_pdf(file_path: str) -> str:
        """從PDF提取文本"""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ImportError("需要安裝PyMuPDF: pip install PyMuPDF")
        
        text_parts = []
        with fitz.open(file_path) as doc:
            for page in doc:
                text = page.get_text()
                if text.strip():
                    text_parts.append(text)
        
        return "\n\n".join(text_parts)
    
    @staticmethod
    def _extract_from_md(file_path: str) -> str:
        """從Markdown提取文本，支持自動編碼檢測"""
        return _read_text_with_fallback(file_path)
    
    @staticmethod
    def _extract_from_txt(file_path: str) -> str:
        """從TXT提取文本，支持自動編碼檢測"""
        return _read_text_with_fallback(file_path)
    
    @classmethod
    def extract_from_multiple(cls, file_paths: List[str]) -> str:
        """
        從多個文件提取文本併合並
        
        Args:
            file_paths: 文件路徑列表
            
        Returns:
            合併後的文本
        """
        all_texts = []
        
        for i, file_path in enumerate(file_paths, 1):
            try:
                text = cls.extract_text(file_path)
                filename = Path(file_path).name
                all_texts.append(f"=== 文檔 {i}: {filename} ===\n{text}")
            except Exception as e:
                all_texts.append(f"=== 文檔 {i}: {file_path} (提取失敗: {str(e)}) ===")
        
        return "\n\n".join(all_texts)


def split_text_into_chunks(
    text: str, 
    chunk_size: int = 500, 
    overlap: int = 50
) -> List[str]:
    """
    將文本分割成小塊
    
    Args:
        text: 原始文本
        chunk_size: 每塊的字符數
        overlap: 重疊字符數
        
    Returns:
        文本塊列表
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []
    
    chunks = []
    start = 0
    
    while start < len(text):
        end = start + chunk_size
        
        # 嘗試在句子邊界處分割
        if end < len(text):
            # 查找最近的句子結束符
            for sep in ['。', '！', '？', '.\n', '!\n', '?\n', '\n\n', '. ', '! ', '? ']:
                last_sep = text[start:end].rfind(sep)
                if last_sep != -1 and last_sep > chunk_size * 0.3:
                    end = start + last_sep + len(sep)
                    break
        
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        
        # 下一個塊從重疊位置開始
        start = end - overlap if end < len(text) else len(text)
    
    return chunks
