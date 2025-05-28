from flask import Flask, request, jsonify, send_file
import xml.etree.ElementTree as ET
import re
import requests
import os
import tempfile
import zipfile
from datetime import datetime
import logging
from werkzeug.utils import secure_filename

# Konfiguracja aplikacji
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB limit

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Konfiguracja Claude API
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

class MUITranslator:
    def __init__(self):
        self.translatable_texts = []
        self.original_structure = None
        self.namespaces = {}
    
    def parse_mui_file(self, file_content):
    """Parsuje plik .mui i wyodrębnia teksty do tłumaczenia"""
        try:
            # Usuń BOM jeśli istnieje
            if file_content.startswith('\ufeff'):
                file_content = file_content[1:]
        
            # Usuń inne problematyczne znaki BOM
            file_content = file_content.replace('\ufeff', '')
        
            # Obsługa namespace XML
            root = ET.fromstring(file_content)
            self.original_structure = file_content
            
            # Znajdowanie wszystkich elementów z tekstem
            translatable_elements = []
            
            def extract_texts(element, path=""):
                for child in element:
                    current_path = f"{path}/{child.tag}" if path else child.tag
                    
                    # Jeśli element ma tekst (nie tylko dzieci)
                    if child.text and child.text.strip():
                        text = child.text.strip()
                        # Pomijamy elementy które wyglądają jak kody/ID
                        if not self.is_code_like(text):
                            translatable_elements.append({
                                'path': current_path,
                                'tag': child.tag,
                                'original_text': text,
                                'element_full_match': f"<{child.tag}>{text}</{child.tag}>"
                            })
                    
                    # Rekurencyjnie przeszukuj dzieci
                    extract_texts(child, current_path)
            
            extract_texts(root)
            self.translatable_texts = translatable_elements
            
            logger.info(f"Znaleziono {len(translatable_elements)} tekstów do tłumaczenia")
            return translatable_elements
            
        except ET.ParseError as e:
            logger.error(f"Błąd parsowania XML: {e}")
            raise Exception(f"Nieprawidłowy format pliku .mui: {e}")
    
    def is_code_like(self, text):
        """Sprawdza czy tekst wygląda jak kod/ID który nie powinien być tłumaczony"""
        # Wzorce które wyglądają jak kody
        code_patterns = [
            r'^[A-Z_][A-Z0-9_]*$',  # UPPER_CASE_CONSTANTS
            r'^\d+$',  # same cyfry
            r'^[a-zA-Z0-9._-]+\.[a-zA-Z0-9._-]+$',  # zawiera kropki jak ID
            r'^#[0-9A-Fa-f]+$',  # hex colors
            r'^\$[a-zA-Z_]',  # zmienne zaczynające się od $
        ]
        
        for pattern in code_patterns:
            if re.match(pattern, text):
                return True
        
        # Teksty krótsze niż 2 znaki prawdopodobnie to kody
        if len(text) < 2:
            return True
            
        return False
    
    def translate_texts(self, texts_to_translate):
        """Tłumaczy teksty przy użyciu Claude API"""
        if not CLAUDE_API_KEY:
            raise Exception("Brak klucza API Claude. Ustaw zmienną CLAUDE_API_KEY.")
        
        # Przygotowanie promptu
        texts_list = [item['original_text'] for item in texts_to_translate]
        
        prompt = f"""Jesteś ekspertem tłumaczeń technicznych dla oprogramowania maszyn laserowych (wycinarki, grawerki).
Tłumacz z angielskiego na polski zachowując:
- Kontekst techniczny przemysłu laserowego i CNC
- Terminologię branżową (np. Pierce Method = Metoda przebicia, Laser Type = Typ lasera)
- Spójność z oprogramowaniem CNC/CAM
- Naturalność języka polskiego

WAŻNE ZASADY:
- Tłumacz TYLKO znaczenie, zachowaj długość podobną do oryginału
- Używaj polskich terminów technicznych z branży laserowej
- Nie dodawaj wyjaśnień ani komentarzy
- Zachowaj styl UI (krótkie, zwięzłe etykiety)

Teksty do tłumaczenia (po jednym w linii):
{chr(10).join(texts_list)}

Zwróć TYLKO przetłumaczone teksty w tej samej kolejności, po jednym w linii:"""

        try:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01"
            }
            
            data = {
                "model": "claude-3-sonnet-20240229",
                "max_tokens": 2000,
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }
            
            response = requests.post(CLAUDE_API_URL, headers=headers, json=data)
            
            if response.status_code != 200:
                logger.error(f"Błąd API Claude: {response.status_code} - {response.text}")
                raise Exception(f"Błąd tłumaczenia: {response.status_code}")
            
            result = response.json()
            translated_text = result['content'][0]['text'].strip()
            
            # Podziel tłumaczenia na linie
            translations = [line.strip() for line in translated_text.split('\n') if line.strip()]
            
            if len(translations) != len(texts_list):
                logger.warning(f"Liczba tłumaczeń ({len(translations)}) nie zgadza się z liczbą tekstów ({len(texts_list)})")
                # Dopasuj długości
                while len(translations) < len(texts_list):
                    translations.append(texts_list[len(translations)])  # Użyj oryginału jako fallback
            
            return translations
            
        except Exception as e:
            logger.error(f"Błąd podczas tłumaczenia: {e}")
            raise Exception(f"Nie udało się przetłumaczyć tekstów: {e}")
    
    def reconstruct_mui_file(self, translations):
        """Wstawia przetłumaczone teksty z powrotem do oryginalnej struktury"""
        try:
            reconstructed_content = self.original_structure
            
            # Zamień każdy tekst na tłumaczenie
            for i, item in enumerate(self.translatable_texts):
                if i < len(translations):
                    original_match = item['element_full_match']
                    translated_match = f"<{item['tag']}>{translations[i]}</{item['tag']}>"
                    
                    # Zamień pierwsze wystąpienie (bezpieczniej niż replace all)
                    reconstructed_content = reconstructed_content.replace(original_match, translated_match, 1)
            
            # Walidacja - sprawdź czy struktura nadal jest poprawna
            try:
                ET.fromstring(reconstructed_content)
                logger.info("Struktura XML zwalidowana pomyślnie")
            except ET.ParseError as e:
                logger.error(f"Błąd walidacji zrekonstruowanego XML: {e}")
                raise Exception("Błąd rekonstrukcji - struktura pliku została uszkodzona")
            
            return reconstructed_content
            
        except Exception as e:
            logger.error(f"Błąd rekonstrukcji pliku: {e}")
            raise Exception(f"Nie udało się zrekonstruować pliku: {e}")
    
    def generate_translation_report(self, translations):
        """Generuje raport z przeprowadzonych tłumaczeń"""
        report_lines = [
            "=== RAPORT TŁUMACZENIA PLIKU .MUI ===",
            f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Liczba przetłumaczonych elementów: {len(translations)}",
            "",
            "SZCZEGÓŁY TŁUMACZEŃ:",
            ""
        ]
        
        for i, item in enumerate(self.translatable_texts):
            if i < len(translations):
                report_lines.extend([
                    f"Element: {item['tag']}",
                    f"Oryginał: {item['original_text']}",
                    f"Tłumaczenie: {translations[i]}",
                    f"Ścieżka: {item['path']}",
                    "---"
                ])
        
        return "\n".join(report_lines)

# Endpoints Flask
@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint do sprawdzania stanu serwisu"""
    return jsonify({
        "status": "healthy",
        "service": "MUI Translator",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/translate', methods=['POST'])
def translate_mui_file():
    """Główny endpoint do tłumaczenia plików .mui"""
    try:
        # Sprawdź czy plik został przesłany
        if 'file' not in request.files:
            return jsonify({"error": "Brak pliku w żądaniu"}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "Nie wybrano pliku"}), 400
        
        # Sprawdź rozszerzenie pliku
        if not file.filename.lower().endswith('.mui'):
            return jsonify({"error": "Plik musi mieć rozszerzenie .mui"}), 400
        
        # Wczytaj zawartość pliku
        file_content = file.read().decode('utf-8')
        
        # Stwórz translator i przetwórz plik
        translator = MUITranslator()
        
        # Krok 1: Parsowanie
        logger.info("Rozpoczynam parsowanie pliku .mui")
        translatable_texts = translator.parse_mui_file(file_content)
        
        if not translatable_texts:
            return jsonify({"error": "Nie znaleziono tekstów do tłumaczenia w pliku"}), 400
        
        # Krok 2: Tłumaczenie
        logger.info(f"Rozpoczynam tłumaczenie {len(translatable_texts)} tekstów")
        translations = translator.translate_texts(translatable_texts)
        
        # Krok 3: Rekonstrukcja
        logger.info("Rekonstruuję plik z tłumaczeniami")
        reconstructed_content = translator.reconstruct_mui_file(translations)
        
        # Krok 4: Generowanie raportu
        report = translator.generate_translation_report(translations)
        
        # Przygotowanie plików do zwrócenia
        with tempfile.TemporaryDirectory() as temp_dir:
            # Zapisz przetłumaczony plik
            translated_filename = f"translated_{secure_filename(file.filename)}"
            translated_path = os.path.join(temp_dir, translated_filename)
            
            with open(translated_path, 'w', encoding='utf-8') as f:
                f.write(reconstructed_content)
            
            # Zapisz raport
            report_filename = f"raport_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            report_path = os.path.join(temp_dir, report_filename)
            
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            
            # Stwórz archiwum ZIP z obydwoma plikami
            zip_filename = f"tlumaczenie_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            zip_path = os.path.join(temp_dir, zip_filename)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(translated_path, translated_filename)
                zipf.write(report_path, report_filename)
            
            # Zwróć archiwum
            return send_file(
                zip_path,
                as_attachment=True,
                download_name=zip_filename,
                mimetype='application/zip'
            )
    
    except Exception as e:
        logger.error(f"Błąd podczas przetwarzania: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/test', methods=['GET'])
def test_endpoint():
    """Endpoint testowy"""
    return jsonify({
        "message": "System MUI Translator działa poprawnie!",
        "claude_api_configured": bool(CLAUDE_API_KEY),
        "timestamp": datetime.now().isoformat()
    })

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Plik jest za duży. Maksymalny rozmiar to 10MB."}), 413

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Wewnętrzny błąd serwera"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
