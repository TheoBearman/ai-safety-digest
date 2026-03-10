"""Research relevance filter for the AI Safety Digest pipeline.

Scores papers based on title + abstract content to filter out non-research
items (news, opinion, commentary). Called by ``fetch.py`` after deduplication
but before abstract enrichment.
"""

from __future__ import annotations

import logging
import re

from scripts.models import Paper

logger = logging.getLogger(__name__)

# Terms that indicate research content (checked against lowercased title + abstract)
RESEARCH_TERMS: list[str] = [
    # Core ML/AI terminology
    "paper", "study", "model", "training", "benchmark", "evaluation",
    "dataset", "algorithm", "framework", "architecture", "fine-tuning",
    "fine tuning", "finetuning", "rlhf", "interpretability", "mechanistic",
    "alignment", "probe", "ablation", "embedding", "transformer", "neural",
    "gradient", "loss", "optimization", "inference", "scaling law",
    "emergent", "capability", "reinforcement learning", "supervised learning",
    "pretraining", "pre-training", "backpropagation", "attention mechanism",
    "language model", "diffusion", "generative", "classification",
    "regression", "tokenizer", "tokenization", "perplexity", "accuracy",
    "precision", "recall", "f1 score", "auc", "roc", "cross-entropy",
    "softmax", "activation", "layer", "hidden state", "representation",
    "latent", "feature", "weight", "parameter", "hyperparameter",
    "convergence", "overfit", "regularization", "dropout", "batch",
    "epoch", "learning rate", "sgd", "adam", "llm", "gpt", "bert",
    # Safety-specific
    "red team", "red-team", "safety", "robustness", "adversarial",
    "reward model", "constitutional", "jailbreak", "guardrail",
    "watermark", "detection", "deception", "sycophancy", "power-seeking",
    "corrigibility", "oversight", "monitor", "audit", "specification",
    # Academic indicators
    "arxiv", "abstract", "methodology", "experiment", "result",
    "finding", "contribution", "technical report", "system card",
    "we propose", "we present", "we show", "we demonstrate",
    "we introduce", "we evaluate", "we find", "our method",
    "our approach", "our model", "state-of-the-art", "sota",
    "baseline", "comparison", "ablation study", "empirical",
    "theoretical", "formal", "proof", "theorem", "lemma",
    "proposition", "corollary", "analysis", "measurement",
    "quantitative", "qualitative", "survey", "review",
    # Infrastructure
    "compute", "flops", "gpu", "tpu", "distributed training",
    "data augmentation", "curriculum learning", "knowledge distillation",
    "multi-task", "transfer learning", "zero-shot", "few-shot",
    "in-context learning", "chain of thought", "prompting",
]

# Organizations known to produce research — given a lower relevance threshold
RESEARCH_ORGS: set[str] = {
    "anthropic", "openai", "google deepmind", "microsoft research",
    "redwood research", "alignment forum", "metr", "apollo research",
    "arc", "miri", "cais", "far ai", "uk aisi", "us aisi",
    "epoch ai", "chai", "mats", "govai", "cset", "iaps", "cltr",
    "rand", "dan hendrycks", "paul christiano", "yoshua bengio",
    "lennart heim", "fli", "lesswrong", "semianalysis", "forethought",
    "zvi mowshowitz", "dean ball", "seb krier", "peter wildeford",
    "ajeya cotra", "cnas",
}

# Curated voices — always included (bypass research-term scoring).
# These are hand-picked authors who primarily write about AI.
ALWAYS_INCLUDE_ORGS: set[str] = {
    "dean ball", "seb krier", "peter wildeford", "ajeya cotra",
}

# Minimum matching terms to keep a paper
THRESHOLD_DEFAULT = 2       # general sources
THRESHOLD_RESEARCH_ORG = 1  # known research orgs


# Title patterns that indicate non-research content (org updates, policy, hiring, etc.)
_NON_RESEARCH_TITLE_RE = re.compile(
    r"\b(?:"
    r"how we protect|confidential information|hiring|join our team|we'?re hiring|"
    r"job opening|careers at|annual report|year in review|"
    r"introducing our team|meet the team|"
    r"donor|fundrais|thank you|update on our org"
    r")\b",
    re.IGNORECASE,
)


def is_research_relevant(paper: Paper) -> bool:
    """Return True if a paper should be kept based on research relevance.

    - Papers with non-research titles (org updates, hiring, etc.) are rejected.
    - Curated voices are always included.
    - arXiv papers and known research org papers need >= 1 matching term.
    - All others need >= 2 matching terms.
    """
    # Reject obviously non-research titles regardless of source
    if _NON_RESEARCH_TITLE_RE.search(paper.title):
        logger.info("Filtered non-research title: %s", paper.title[:80])
        return False

    org_lower = paper.organization.lower()

    # Curated voices are always included
    if org_lower in ALWAYS_INCLUDE_ORGS:
        return True

    searchable = (paper.title + " " + paper.abstract).lower()
    score = sum(1 for term in RESEARCH_TERMS if term in searchable)

    # arXiv papers are treated like known research orgs (need score >= 1)
    if paper.source_type == "arxiv":
        threshold = THRESHOLD_RESEARCH_ORG
    elif org_lower in RESEARCH_ORGS:
        threshold = THRESHOLD_RESEARCH_ORG
    else:
        threshold = THRESHOLD_DEFAULT

    return score >= threshold


def filter_papers(papers: list[Paper]) -> list[Paper]:
    """Filter a list of papers for research relevance. Logs stats."""
    before = len(papers)
    result = [p for p in papers if is_research_relevant(p)]
    removed = before - len(result)
    logger.info(
        "Research filter: kept %d, removed %d of %d",
        len(result), removed, before,
    )
    return result
