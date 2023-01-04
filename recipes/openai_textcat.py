import copy
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

import httpx
import jinja2
import prodigy
import prodigy.components.db
import prodigy.components.preprocess
import prodigy.components.sorters
import prodigy.models.textcat
import prodigy.types
import prodigy.util
import rich
import spacy
import srsly
import tqdm
from dotenv import load_dotenv
from prodigy.util import msg, log
from rich.panel import Panel
from spacy.language import Language


_ItemT = TypeVar("_ItemT")

DEFAULT_PROMPT_PATH = (
    Path(__file__).parent.parent / "templates" / "textcat_prompt.jinja2"
)
CSS_FILE_PATH = Path(__file__).parent / "style.css"

TEXTCAT_LABEL = "Recipe"

# Set up openai access by taking environment variables from .env.
load_dotenv()

HTML_TEMPLATE = """
<div class="cleaned">
  <details>
    <summary>Show the prompt for OpenAI</summary>
    <pre>{{openai.prompt}}</pre>
  </details>
  <details>
    <summary>Show the response from OpenAI</summary>
    <pre>{{openai.response}}</pre>
  </details>
</div>
"""

# Set up openai
load_dotenv()  # take environment variables from .env.


@dataclass
class PromptExample:
    """An example to be passed into an OpenAI TextCat prompt."""

    text: str
    label: str = TEXTCAT_LABEL

    @staticmethod
    def is_flagged(example: Dict) -> bool:
        """Check whether a Prodigy example is flagged for use
        in the prompt."""

        return (
            example.get("flagged") is True
            and example.get("answer") == "accept"
            and "text" in example
        )

    @classmethod
    def from_prodigy(cls, example: Dict, labels: Iterable[str]) -> "PromptExample":
        """Create a prompt example from Prodigy's format.
        Only entities with a label from the given set will be retained.
        The given set of labels is assumed to be already normalized.
        """
        if "text" not in example:
            raise ValueError("Cannot make PromptExample without text")

        full_text = example["text"]
        label = example["label"]
        return cls(text=full_text, label=_normalize_label(label))


def _normalize_label(label: str) -> str:
    return label.lower()


class OpenAISuggester:
    prompt_template: jinja2.Template
    model: str
    labels: List[str]
    max_examples: int
    segment: bool
    verbose: bool
    openai_api_org: str
    openai_api_key: str
    openai_temperature: int
    openai_max_tokens: int
    openai_timeout_s: int
    openai_n: int
    examples: List[PromptExample]

    def __init__(
        self,
        prompt_template: jinja2.Template,
        *,
        labels: List[str],
        max_examples: int,
        segment: bool,
        openai_api_org: str,
        openai_api_key: str,
        openai_model: str,
        openai_temperature: int = 0,
        openai_max_tokens: int = 500,
        openai_timeout_s: int = 1,
        openai_n: int = 1,
        verbose: bool = False,
    ):
        self.prompt_template = prompt_template
        self.model = openai_model
        self.labels = [_normalize_label(label) for label in labels]
        self.max_examples = max_examples
        self.verbose = verbose
        self.segment = segment
        self.examples = []
        self.openai_api_org = openai_api_org
        self.openai_api_key = openai_api_key
        self.openai_temperature = openai_temperature
        self.openai_max_tokens = openai_max_tokens
        self.openai_timeout_s = openai_timeout_s
        self.openai_n = openai_n

    def __call__(
        self, stream: Iterable[Dict], *, nlp: Language, batch_size: int
    ) -> Iterable[Dict]:
        if self.segment:
            stream = prodigy.components.preprocess.split_sentences(nlp, stream)

        stream = self.stream_suggestions(stream, batch_size=batch_size)
        stream = self.format_suggestions(stream, nlp=nlp)
        return stream

    def update(self, examples: Iterable[Dict]) -> float:
        for eg in examples:
            if PromptExample.is_flagged(eg):
                self.add_example(PromptExample.from_prodigy(eg, self.labels))
        return 0.0

    def add_example(self, example: PromptExample) -> None:
        """Add an example for use in the prompts. Examples are pruned to the most recent max_examples."""
        if self.max_examples:
            self.examples.append(example)
        if len(self.examples) >= self.max_examples:
            self.examples = self.examples[-self.max_examples :]

    def stream_suggestions(
        self, stream: Iterable[Dict], batch_size: int
    ) -> Iterable[Dict]:
        """Get zero-shot or few-shot suggested TextCat annotations from OpenAI.

        Given a stream of input examples, we define a prompt, get a response from OpenAI,
        and yield each example with their predictions to the output stream.
        """
        for batch in _batch_sequence(stream, batch_size):
            prompts = [
                self._get_textcat_prompt(
                    eg["text"], labels=self.labels, examples=self.examples
                )
                for eg in batch
            ]
            responses = self._get_textcat_response(prompts)
            for eg, prompt, response in zip(batch, prompts, responses):
                if self.verbose:
                    rich.print(Panel(prompt, title="Prompt to OpenAI"))
                eg["openai"] = {"prompt": prompt, "response": response}
                if self.verbose:
                    rich.print(Panel(response, title="Response from OpenAI"))
                yield eg

    def format_suggestions(
        self, stream: Iterable[Dict], *, nlp: Language
    ) -> Iterable[Dict]:
        """Parse the examples in the stream and set up span annotations
        to display in the Prodigy UI.
        """
        stream = prodigy.components.preprocess.add_tokens(nlp, stream, skip=True)  # type: ignore
        for example in stream:
            example = copy.deepcopy(example)
            # This tokenizes the text with spaCy, so that annotations on the Prodigy UI
            # can automatically snap to token boundaries, making the process much more efficient.
            doc = nlp.make_doc(example["text"])
            response = self._parse_response(example["openai"]["response"])
            example["chatgpt_answer"] = response["answer"] == "accept"
            yield prodigy.util.set_hashes(example)

    def _get_textcat_prompt(
        self, text: str, labels: List[str], examples: List[PromptExample]
    ) -> str:
        """Generate a prompt for text categorization.

        The prompt can use examples to further clarify the task. Note that using too
        many examples will make the prompt too large, slowing things down.
        """
        # TODO: Update this once you have "finalized" your prompt
        return self.prompt_template.render(text=text, labels=labels, examples=examples)

    def _get_textcat_response(self, prompts: List[str]) -> List[str]:
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "OpenAI-Organization": self.openai_api_org,
            "Content-Type": "application/json",
        }
        r = _retry429(
            lambda: httpx.post(
                "https://api.openai.com/v1/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "prompt": prompts,
                    "temperature": self.openai_temperature,
                    "max_tokens": self.openai_max_tokens,
                },
            ),
            n=self.openai_n,
            timeout_s=self.openai_timeout_s,
        )
        r.raise_for_status()
        responses = r.json()
        return [responses["choices"][i]["text"] for i in range(len(prompts))]

    # TODO: Check sample OpenAI response first. Should probably return a str ("accept/reject")
    def _parse_response(self, text: str) -> List[Tuple[str, List[str]]]:
        """Interpret OpenAI's TextCat response. It's supposed to be
        a list of lines, with each line having the form:
        Label: phrase1, phrase2, ...

        However, there's no guarantee that the model will give
        us well-formed output. It could say anything, it's an LM.
        So we need to be robust.
        """
        output = []
        for line in text.strip().split("\n"):
            if line and ":" in line:
                label, phrases = line.split(":", 1)
                label = _normalize_label(label)
                if label in self.labels:
                    if phrases.strip():
                        phrases = [
                            phrase.strip() for phrase in phrases.strip().split(",")
                        ]
                        output.append((label, phrases))
        return output


@prodigy.recipe(
    "textcat.openai.teach",
    input_path=("Path to jsonl data to annotate", "positional", None, Path),
    output_path=("Path to save the output", "positional", None, Path),
    labels=("Labels (comma delimited)", "positional", None, lambda s: s.split(",")),
    lang=("Language to use for tokenizer.", "option", "l", str),
    model=("GPT-3 model to use for completion", "option", "m", str),
    examples_path=("Examples file to help define the task", "option", "e", Path),
    max_examples=("Max examples to include in prompt", "option", "n", int),
    prompt_path=("Path to jinja2 prompt template", "option", "p", Path),
    batch_size=("Batch size to send to OpenAI API", "option", "b", int),
    segment=("Split sentences", "flag", "S", bool),
    verbose=("Print extra information to terminal", "option", "flag", bool),
)
def textcat_openai_teach(
    dataset: str,
    input_path: Path,
    spacy_model: str,
    label: List[str],
    chatgpt_bias: float = 0.5,
    model: str = "text-davinci-003",
    batch_size: int = 10,
    segment: bool = False,
    examples_path: Optional[Path] = None,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
    max_examples: int = 2,
    verbose: bool = False,
):
    """Get bulk TextCat suggestions from an OpenAI API, using zero-shot or
    few-shot learning.  The results can then be corrected using the
    `textcat.manual` recipe.

    Here, we use ChatGPT to suggest if a particular text talks about a recipe or
    not. You can use the parameter `--chatgpt-bias` (float, 0 to 1 inclusive) to
    set how much we prefer getting examples that ChatGPT thinks as recipes.

    This approach lets you get the OpenAI queries out of the way upfront, which
    can help if you want to use multiple annotators of if you want to make sure
    you don't have to wait on the OpenAI queries.
    """

    api_key, api_org = _get_api_credentials(model)
    examples = _read_prompt_examples(examples_path)
    if label is None:
        msg.fail("textcat.teach requires at least one --label", exits=1)
    nlp = spacy.load(spacy_model)
    name = prodigy.models.textcat.add_text_classifier(nlp, label)
    model = prodigy.models.textcat.TextClassifier(nlp=nlp, labels=label, pipe_name=name)
    log(f"RECIPE: Creating TextClassifier with model {spacy_model}")

    if segment:
        nlp.add_pipe("sentencizer")

    openai = OpenAISuggester(
        openai_model=model,
        labels=label,
        max_examples=max_examples,
        prompt_template=_load_template(prompt_path),
        verbose=verbose,
        segment=segment,
        openai_api_key=api_key,
        openai_api_org=api_org,
    )

    for eg in examples:
        openai.add_example(eg)

    stream = list(srsly.read_jsonl(input_path))
    stream = openai(tqdm.tqdm(stream), batch_size=batch_size, nlp=nlp)

    # Setup update loop
    predict = model
    stream = _prefer_gpt(predict(stream), chatgpt_bias)

    return {
        "dataset": dataset,
        "view_id": "blocks",
        "stream": stream,
        "update": openai.update,
        "config": {
            "labels": openai.labels,
            "batch_size": batch_size,
            "exclude_by": "input",
            "blocks": [
                {"view_id": "classification"},
                {"view_id": "html", "html_template": HTML_TEMPLATE},
            ],
            "show_flag": True,
            "global_css": CSS_FILE_PATH.read_text(),
        },
    }


def _prefer_gpt(stream, bias: float) -> Iterable[Dict]:
    log(f"SORTER: Resort stream to prefer positive classes (bias: {bias})")
    sorted_stream = (
        (prodigy.components.sorters.get_uncertainty(score, bias), eg)
        if eg.get("chatgpt_answer")
        else (prodigy.components.sorters.get_uncertainty(score, 1 - bias), eg)
        for score, eg in stream
    )
    return prodigy.components.sorters.Probability(sorted_stream)


def _get_api_credentials(model: str = None) -> Tuple[str, str]:
    # Fetch and check the key
    api_key = os.getenv("OPENAI_KEY")
    if api_key is None:
        m = (
            "Could not find the API key to access the openai API. Ensure you have an API key "
            "set up via https://beta.openai.com/account/api-keys, then make it available as "
            "an environment variable 'OPENAI_KEY', for instance in a .env file."
        )
        msg.fail(m)
        sys.exit(-1)
    # Fetch and check the org
    org = os.getenv("OPENAI_ORG")
    if org is None:
        m = (
            "Could not find the organisation to access the openai API. Ensure you have an API key "
            "set up via https://beta.openai.com/account/api-keys, obtain its organization ID 'org-XXX' "
            "via https://beta.openai.com/account/org-settings, then make it available as "
            "an environment variable 'OPENAI_ORG', for instance in a .env file."
        )
        msg.fail(m)
        sys.exit(-1)

    # Check the access and get a list of available models to verify the model argument (if not None)
    # Even if the model is None, this call is used as a healthcheck to verify access.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Organization": org,
    }
    r = _retry429(
        lambda: httpx.get(
            "https://api.openai.com/v1/models",
            headers=headers,
        ),
        n=1,
        timeout_s=1,
    )
    if r.status_code == 422:
        m = (
            "Could not access api.openai.com -- 422 permission denied."
            "Visit https://beta.openai.com/account/api-keys to check your API keys."
        )
        msg.fail(m)
        sys.exit(-1)
    elif r.status_code != 200:
        m = "Error accessing api.openai.com" f"{r.status_code}: {r.text}"
        msg.fail(m)
        sys.exit(-1)

    if model is not None:
        response = r.json()["data"]
        models = [response[i]["id"] for i in range(len(response))]
        if model not in models:
            e = f"The specified model '{model}' is not available. Choices are: {sorted(set(models))}"
            msg.fail(e, exits=1)

    return api_key, org


def _read_prompt_examples(path: Optional[Path]) -> List[PromptExample]:
    if path is None:
        return []
    elif path.suffix in (".yml", ".yaml"):
        return _read_yaml_examples(path)
    elif path.suffix == ".json":
        data = srsly.read_json(path)
        assert isinstance(data, list)
        return [PromptExample(**eg) for eg in data]
    else:
        msg.fail(
            "The --examples-path (-e) parameter expects a .yml, .yaml or .json file."
        )
        sys.exit(-1)


def _load_template(path: Path) -> jinja2.Template:
    # I know jinja has a lot of complex file loading stuff,
    # but we're not using the inheritance etc that makes
    # that stuff worthwhile.
    if not path.suffix == ".jinja2":
        msg.fail(
            "The --prompt-path (-p) parameter expects a .jinja2 file.",
            exits=1,
        )
    with path.open("r", encoding="utf8") as file_:
        text = file_.read()
    return jinja2.Template(text)


def _retry429(
    call_api: Callable[[], httpx.Response], n: int, timeout_s: int
) -> httpx.Response:
    """Retry a call to the OpenAI API if we get a 429: Too many requests
    error.
    """
    assert n >= 0
    assert timeout_s >= 1
    r = call_api()
    i = -1
    while i < n and r.status_code == 429:
        time.sleep(timeout_s)
        i += 1
    return r


def _read_yaml_examples(path: Path) -> List[PromptExample]:
    data = srsly.read_yaml(path)
    if not isinstance(data, list):
        msg.fail("Cannot interpret prompt examples from yaml", exits=True)
    assert isinstance(data, list)
    output = []
    for item in data:
        output.append(PromptExample(text=item["text"], entities=item["entities"]))
    return output


def _batch_sequence(items: Iterable[_ItemT], batch_size: int) -> Iterable[List[_ItemT]]:
    batch = []
    for eg in items:
        batch.append(eg)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch