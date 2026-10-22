"""Shared test helper: adds the review issues a draft owes for its missing or contextual fields."""


def add_missing_issues(output):
    def walk(value, path=""):
        if isinstance(value, dict):
            if value.get("basis") in {"missing", "contextual"}:
                output["review_issues"].append({"target": path, "kind": "missing" if value["basis"] == "missing" else "contextual",
                                               "reason": "Source coverage or contextual attribution requires review.",
                                               "evidence": []})
            for k, v in value.items():
                if k != "review_issues":
                    walk(v, path + "/" + k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, path + "/" + str(i))
    walk(output)
