from app.config import Settings
import app.services.llm as llm_module


def test_mode_selection(monkeypatch):
    settings_azure = Settings(
        _env_file=None,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="key",
    )
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings_azure)
    service = llm_module.LLMService()
    assert service.mode == "azure:gpt-5.5"
    assert service._azure is True

    settings_deepseek = Settings(_env_file=None, deepseek_api_key="dk")
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings_deepseek)
    service = llm_module.LLMService()
    assert service.mode == "deepseek"

    settings_local = Settings(_env_file=None)
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings_local)
    service = llm_module.LLMService()
    assert service.mode == "local"
    # verification fails open only in local mode (there is no reviewer)
    verdict = service.verify_fix({}, "diff", "logs")
    assert verdict["approved"] is True
