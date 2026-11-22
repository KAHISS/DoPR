from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequestDraft:
    title: str
    body: str

    def as_markdown(self) -> str:
        return f"# {self.title}\n\n{self.body}"
