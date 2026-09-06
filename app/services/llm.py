import json
import re
from typing import Any

from openai import AzureOpenAI, OpenAI

from ..config import get_settings


class LLMService:
    """LLM access. Prefers an Azure OpenAI deployment (e.g. gpt-5.5), falls
    back to DeepSeek, then to keyword heuristics when neither is configured."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = None
        self.model = ""
        self._azure = False
        if self.settings.azure_openai_configured:
            self.client = AzureOpenAI(
                api_key=self.settings.azure_openai_api_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
            )
            self.model = self.settings.azure_openai_deployment
            self._azure = True
        elif self.settings.deepseek_api_key:
            self.client = OpenAI(
                api_key=self.settings.deepseek_api_key,
                base_url="https://api.deepseek.com",
            )
            self.model = self.settings.deepseek_model

    @property
    def mode(self) -> str:
        if not self.client:
            return "local"
        return f"azure:{self.model}" if self._azure else "deepseek"

    def _chat_json(self, system: str, prompt: str) -> dict[str, Any]:
        """One JSON-mode chat completion; raises on missing client/invalid JSON."""
        if not self.client:
            raise RuntimeError(
                "Configure AZURE_OPENAI_* (or DEEPSEEK_API_KEY) to generate code fixes"
            )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if self._azure:
            # GPT-5-family deployments reject custom temperature and use
            # max_completion_tokens instead of max_tokens.
            kwargs["max_completion_tokens"] = 16384
        else:
            kwargs["temperature"] = 0.1
            kwargs["max_tokens"] = 8192
        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        return json.loads(content)

    def structured(self, system: str, prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
        if not self.client:
            return fallback
        try:
            return self._chat_json(system, prompt)
        except Exception:
            return fallback

    def classify(self, title: str, description: str, logs: str, service_hint: str | None) -> dict[str, str]:
        text = f"{title}\n{description}\n{logs}".lower()
        service = service_hint or self._detect_service(text)
        severity = "SEV-1" if any(word in text for word in ("outage", "all users", "data loss")) else (
            "SEV-2" if any(word in text for word in ("500", "failed", "unavailable", "timeout")) else "SEV-3"
        )
        category, root_type = self._detect_category(text)
        fallback = {
            "service": service,
            "severity": severity,
            "error_category": category,
            "root_cause_type": root_type,
        }
        return self.structured(
            "Classify production incidents. Return only JSON with service, severity, error_category, root_cause_type.",
            f"Title: {title}\nDescription: {description}\nLogs:\n{logs}",
            fallback,
        )

    def diagnose(self, incident: dict[str, Any], memories: list[dict[str, Any]]) -> dict[str, Any]:
        best = memories[0] if memories else None
        fallback = self._fallback_diagnosis(incident, best)
        memory_context = json.dumps(memories[:5], default=str)
        return self.structured(
            (
                "You are a senior SRE. Diagnose from evidence, distinguish facts from hypotheses, "
                "and return JSON with diagnosis, root_cause, impact, resolution_steps, action_items."
            ),
            f"Current incident:\n{json.dumps(incident)}\nSimilar incidents:\n{memory_context}",
            fallback,
        )

    def fix_suggestion(
        self,
        incident: dict[str, Any],
        source: str,
        file_path: str,
    ) -> dict[str, str]:
        result = self.structured(
            (
                "You are a senior production engineer. Return JSON with summary, rationale, "
                "diff, fixed_content. Preserve unrelated code. fixed_content must contain the "
                "complete corrected file without markdown. diff must be unified diff format."
            ),
            f"Incident: {json.dumps(incident)}\nFile: {file_path}\nSource:\n{source[:30000]}",
            {},
        )
        required = {"summary", "rationale", "diff", "fixed_content"}
        if not required.issubset(result) or not result["fixed_content"]:
            raise RuntimeError("The LLM returned an incomplete code fix")
        return {key: str(result[key]) for key in required}

    def fix_files(
        self,
        incident: dict[str, Any],
        files: list[dict[str, str]],
        file_tree: list[str],
        logs: str,
        commit_diff: str = "",
    ) -> dict[str, Any]:
        """Multi-file fix for the clone-based fixer. Unlike structured(), this
        fails loudly: a swallowed error would silently produce an empty PR."""
        file_sections = "\n\n".join(
            f"=== {item['path']} ===\n{item['content'][:30000]}" for item in files
        )
        prompt = (
            f"Incident diagnosis:\n{json.dumps({k: incident.get(k) for k in ('title', 'description', 'diagnosis', 'root_cause', 'error_category', 'service')})}\n\n"
            f"Error logs (tail):\n{logs[-20000:]}\n\n"
            + (f"Last commit diff:\n{commit_diff[-20000:]}\n\n" if commit_diff else "")
            + f"Repository file tree (partial):\n{chr(10).join(file_tree[:400])}\n\n"
            f"Candidate source files:\n{file_sections}"
        )
        response = self.client.chat.completions.create(
            model=self.settings.deepseek_model,
            temperature=0.1,
            max_tokens=8192,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior production engineer fixing the root cause of a "
                        "production incident. Return only JSON: {\"summary\": str, "
                        "\"rationale\": str, \"files\": [{\"path\": str, \"action\": "
                        "\"update\"|\"create\", \"content\": str}]}. Each content value must "
                        "be the complete corrected file with no markdown fences. Change as "
                        "few files and lines as possible; preserve unrelated code exactly."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or ""
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError("DeepSeek returned invalid JSON for the multi-file fix") from error
        files_out = result.get("files")
        if (
            not result.get("summary")
            or not isinstance(files_out, list)
            or not files_out
            or not all(item.get("path") and item.get("content") for item in files_out)
        ):
            raise RuntimeError("DeepSeek returned an incomplete multi-file fix")
        return result

    @staticmethod
    def _detect_service(text: str) -> str:
        known = ["checkout-service", "auth-service", "payments-api", "search-service", "catalog-service"]
        for service in known:
            if service in text:
                return service
        match = re.search(r"([a-z][a-z0-9-]+-(?:service|api))", text)
        return match.group(1) if match else "unknown-service"

    @staticmethod
    def _detect_category(text: str) -> tuple[str, str]:
        rules = [
            (("pool", "connection"), ("database", "connection-pool-exhaustion")),
            (("jwt", "token", "expiry", "expired"), ("authentication", "token-misconfiguration")),
            (("timeout", "upstream"), ("dependency", "third-party-timeout")),
            (("memory", "oom", "heap"), ("resource", "memory-exhaustion")),
            (("rate limit", "429"), ("traffic", "rate-limit")),
            (("migration", "column", "schema"), ("database", "schema-mismatch")),
            (("build", "module not found", "dependency"), ("deployment", "dependency-build-failure")),
        ]
        for keywords, result in rules:
            if any(keyword in text for keyword in keywords):
                return result
        return "application", "unknown"

    @staticmethod
    def _fallback_diagnosis(
        incident: dict[str, Any], memory: dict[str, Any] | None
    ) -> dict[str, Any]:
        if memory:
            reference = memory["incident"]
            score = int(memory["similarity"] * 100)
            return {
                "diagnosis": (
                    f"This incident is a {score}% match to {reference['title']}. "
                    f"The strongest hypothesis is {reference['root_cause']}."
                ),
                "root_cause": reference["root_cause"],
                "impact": "Requests to the affected service may fail or experience elevated latency.",
                "resolution_steps": reference["resolution_steps"],
                "action_items": [
                    f"Add an alert for {incident['root_cause_type']}.",
                    "Validate the mitigation in staging before production rollout.",
                    "Review recurrence across the last 30 days.",
                ],
            }
        return {
            "diagnosis": (
                f"No close memory was found. Initial evidence points to "
                f"{incident['root_cause_type']} in {incident['service']}."
            ),
            "root_cause": f"Suspected {incident['root_cause_type']}; confirmation required.",
            "impact": "Impact is still being assessed.",
            "resolution_steps": [
                "Confirm the first failing deployment or request.",
                "Inspect service logs and recent configuration changes.",
                "Roll back the latest change if customer impact is increasing.",
            ],
            "action_items": ["Capture the confirmed resolution to enrich incident memory."],
        }


llm_service = LLMService()
