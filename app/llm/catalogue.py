"""Provider catalogue shown in the dropdown.

Each entry names the wire protocol the gateway speaks, a default endpoint the administrator can
overwrite, and the default data zone. Five protocols cover hosted APIs, gateways and self-hosted
servers, so new providers are usually a catalogue line, not code.
"""
PROTOCOLS = {"openai", "anthropic", "azure_openai", "gemini", "ollama"}

CATALOGUE = [
    # key, label, protocol, default base URL, default zone, needs key, note
    ("openai", "OpenAI", "openai", "https://api.openai.com/v1", "CLOUD", True, ""),
    ("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com", "CLOUD", True, ""),
    ("azure_openai", "Azure OpenAI", "azure_openai", "https://YOUR-RESOURCE.openai.azure.com", "CLOUD", True,
     "Model = deployment name; set api_version under advanced options."),
    ("google_gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "CLOUD", True, ""),
    ("mistral", "Mistral AI", "openai", "https://api.mistral.ai/v1", "CLOUD", True, ""),
    ("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "CLOUD", True, ""),
    ("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "CLOUD", True, ""),
    ("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "CLOUD", True, ""),
    ("together", "Together AI", "openai", "https://api.together.xyz/v1", "CLOUD", True, ""),
    ("litellm", "LiteLLM / enterprise AI gateway", "openai", "http://localhost:4000/v1", "PRIVATE", False,
     "Use for AWS Bedrock, Vertex AI and other providers behind your own gateway."),
    ("ollama", "Ollama (self-hosted)", "ollama", "http://localhost:11434", "PRIVATE", False, ""),
    ("vllm", "vLLM / TGI (self-hosted, OpenAI-compatible)", "openai", "http://localhost:8001/v1", "PRIVATE", False, ""),
    ("lmstudio", "LM Studio (local)", "openai", "http://localhost:1234/v1", "PRIVATE", False, ""),
    ("custom_openai", "Custom OpenAI-compatible endpoint", "openai", "https://", "CLOUD", False,
     "Any server implementing /chat/completions."),
]

BY_KEY = {c[0]: c for c in CATALOGUE}

PURPOSES = {
    "flowdown": "Flow-down disposition proposals (Contract Intelligence)",
    "knowledge": "Knowledge-query answer drafting (Knowledge Retrieval)",
}


def as_dicts():
    return [{"key": k, "label": label, "protocol": proto, "default_base_url": url, "default_zone": zone,
             "requires_key": needs_key, "note": note} for k, label, proto, url, zone, needs_key, note in CATALOGUE]
