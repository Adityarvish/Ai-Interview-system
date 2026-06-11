import pdfplumber
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class ResumeParser:
    @staticmethod
    def parse_pdf(file_path):
        """Extract text from PDF resume"""
        try:
            text = ""
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            
            logger.info(f"Parsed resume: {file_path}, extracted {len(text)} characters")
            return text.strip()
        except Exception as e:
            logger.error(f"Error parsing PDF: {e}")
            raise
    
    @staticmethod
    def parse_txt(file_path):
        """Extract text from TXT resume"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
            logger.info(f"Parsed text resume: {file_path}")
            return text.strip()
        except Exception as e:
            logger.error(f"Error parsing TXT: {e}")
            raise
    
    @staticmethod
    def parse(file_path):
        """Parse resume based on file extension"""
        path = Path(file_path)
        extension = path.suffix.lower()
        
        if extension == '.pdf':
            return ResumeParser.parse_pdf(file_path)
        elif extension == '.txt':
            return ResumeParser.parse_txt(file_path)
        else:
            raise ValueError(f"Unsupported file format: {extension}")
