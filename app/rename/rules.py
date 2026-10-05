"""Sequential, explicit literal transformations; never executable expressions."""


class RuleEngine:
    @staticmethod
    def apply(name: str, rules: list[dict]) -> str:
        if not isinstance(rules, list) or len(rules) > 100:
            raise ValueError("Правила должны быть списком, не более 100 правил.")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("Каждое правило должно быть JSON-объектом.")
            if rule.get("enabled", True) is False:
                continue
            kind = rule.get("type") or rule.get("action")
            value = str(rule.get("value", rule.get("text", "")))
            if kind == "replace":
                old = str(rule.get("from", rule.get("search", "")))
                if old:
                    name = name.replace(old, str(rule.get("to", rule.get("replacement", ""))))
            elif kind in {"prefix", "add_prefix"}:
                name = value + name
            elif kind in {"suffix", "add_suffix"}:
                name += value
            elif kind in {"remove", "remove_text"}:
                name = name.replace(value, "") if value else name
            elif kind in {"remove_chars", "remove_characters"}:
                chars = set(str(rule.get("characters", value)))
                name = "".join(char for char in name if char not in chars)
            elif kind == "replace_spaces":
                name = name.replace(" ", str(rule.get("to", rule.get("replacement", "_"))))
            else:
                raise ValueError(f"Неизвестное правило: {kind}.")
        return name
