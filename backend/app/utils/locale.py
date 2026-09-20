import contextvars
import json
import os
import threading
from flask import request, has_request_context

_thread_local = threading.local()

_locales_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'locales')

# Load language registry
with open(os.path.join(_locales_dir, 'languages.json'), 'r', encoding='utf-8') as f:
    _languages = json.load(f)

# Load translation files
_translations = {}
for filename in os.listdir(_locales_dir):
    if filename.endswith('.json') and filename != 'languages.json':
        locale_name = filename[:-5]
        with open(os.path.join(_locales_dir, filename), 'r', encoding='utf-8') as f:
            _translations[locale_name] = json.load(f)


def set_locale(locale: str):
    """Set locale for current thread. Call at the start of background threads."""
    _thread_local.locale = locale


def get_locale() -> str:
    if has_request_context():
        raw = request.headers.get('Accept-Language', 'zh')
        return raw if raw in _translations else 'zh'
    return getattr(_thread_local, 'locale', 'zh')


def t(key: str, **kwargs) -> str:
    locale = get_locale()
    messages = _translations.get(locale, _translations.get('zh', {}))

    value = messages
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        value = _translations.get('zh', {})
        for part in key.split('.'):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

    if value is None:
        return key

    if kwargs:
        for k, v in kwargs.items():
            value = value.replace(f'{{{k}}}', str(v))

    return value


FALLBACK_OUTPUT_LANGUAGE = 'id'

# Keputusan produk (final): klien adalah pelaku usaha Indonesia. Bahasa keluaran
# konten yang dihasilkan LLM (report, chat, wawancara, persona, config simulasi,
# ontologi) = param request `language` > env REPORT_OUTPUT_LANGUAGE > default 'id'.
# Accept-Language SENGAJA tidak dipertimbangkan (browser klien Indonesia sering
# en-US). Locale UI (get_locale()/t()) tetap dipakai untuk pesan UI dan TIDAK
# berubah; get_language_instruction() tetap mengikuti locale UI.
_output_language = contextvars.ContextVar('output_language', default=None)


def _valid_language(code) -> bool:
    return isinstance(code, str) and code in _languages


def set_output_language(language):
    """Override bahasa keluaran LLM pada konteks saat ini (None = hapus).
    ContextVar: terisolasi per thread/konteks. Kode tak dikenal diabaikan.
    Panggil di awal thread latar dan reset di finally."""
    _output_language.set(language if _valid_language(language) else None)


def get_output_language() -> str:
    """Bahasa teks keluaran LLM. Prioritas: override > env REPORT_OUTPUT_LANGUAGE > 'id'."""
    override = _output_language.get()
    if _valid_language(override):
        return override
    env = os.environ.get('REPORT_OUTPUT_LANGUAGE', '').strip().lower()
    if _valid_language(env):
        return env
    return FALLBACK_OUTPUT_LANGUAGE


def resolve_output_language(requested=None) -> str:
    """Requested language if valid, else the current default (no mutation)."""
    return requested if _valid_language(requested) else get_output_language()


def get_language_instruction() -> str:
    """Instruksi bahasa berbasis locale UI (perilaku asli, tak berubah)."""
    locale = get_locale()
    lang_config = _languages.get(locale, _languages.get('zh', {}))
    return lang_config.get('llmInstruction', '请使用中文回答。')


def get_output_language_instruction() -> str:
    """Instruksi bahasa untuk keluaran LLM (lihat keputusan produk di atas)."""
    return _languages.get(get_output_language(), {}).get('llmInstruction', '')


def with_language_instruction(prompt: str) -> str:
    """Bungkus system prompt: instruksi bahasa keluaran di AWAL dan AKHIR (prompt
    dasar berbahasa Mandarin; tanpa penekanan di awal model cenderung ikut Mandarin)."""
    instruction = get_output_language_instruction()
    if not instruction:
        return prompt
    return f"{instruction}\n\n{prompt}\n\n{instruction}"


_FALLBACK_OUTLINES = {
    'id': ("Laporan Prediksi Masa Depan", "Analisis tren dan risiko berdasarkan prediksi simulasi",
           ["Skenario Prediksi dan Temuan Utama", "Analisis Prediksi Perilaku Audiens", "Prospek Tren dan Peringatan Risiko"]),
    'en': ("Future Prediction Report", "Trend and risk analysis based on simulation predictions",
           ["Predicted Scenario and Key Findings", "Audience Behavior Prediction Analysis", "Trend Outlook and Risk Warnings"]),
    'zh': ("未来预测报告", "基于模拟预测的未来趋势与风险分析",
           ["预测场景与核心发现", "人群行为预测分析", "趋势展望与风险提示"]),
}


def get_fallback_outline():
    """(title, summary, [section titles]) dalam bahasa keluaran; bahasa lain -> en."""
    return _FALLBACK_OUTLINES.get(get_output_language(), _FALLBACK_OUTLINES['en'])
