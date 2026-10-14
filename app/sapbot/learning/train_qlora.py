"""QLoRA / DPO fine-tuning harness (FSD 3.5.4) - runs on the GPU machine, NOT on consultant laptops.

Dormant template: needs `pip install sapbot[train]` (unsloth, trl, datasets) and an NVIDIA GPU (RTX 4090 / A10G /
L4 class). Fine-tuning only makes sense once a few hundred gold runs exist (ADR build order).

  python -m sapbot.learning.train_qlora --data C:\\SAPBot\\data\\training\\<stamp> --base Qwen/Qwen2.5-14B-Instruct
      --out ./sapbot-lora [--dpo] [--gguf]

Output: a LoRA adapter (and optionally GGUF q4_k_m) to serve with vLLM (--enable-lora) or Ollama.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger("sapbot.train")


def _load_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def main() -> None:  # pragma: no cover - needs GPU
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--base", default="Qwen/Qwen2.5-14B-Instruct")
    ap.add_argument("--out", default="sapbot-lora")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--max-seq", type=int, default=8192)
    ap.add_argument("--dpo", action="store_true", help="run a DPO pass after SFT using dpo_pairs.jsonl")
    ap.add_argument("--gguf", action="store_true", help="also export GGUF q4_k_m for Ollama")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastLanguageModel

    data = Path(a.data)
    sft_rows = _load_jsonl(data / "sft_gold.jsonl")
    if len(sft_rows) < 50:
        log.warning("only %d gold examples - expect little gain; RAG is the better lever for now", len(sft_rows))
    model, tok = FastLanguageModel.from_pretrained(a.base, max_seq_length=a.max_seq, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(model, r=16, lora_alpha=32, lora_dropout=0.0,
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj",
                                                             "up_proj", "down_proj"])

    def fmt(rec: dict) -> dict:
        return {"text": tok.apply_chat_template(rec["messages"], tools=[t["function"] for t in rec.get("tools", [])],
                                                tokenize=False)}
    ds = Dataset.from_list([fmt(r) for r in sft_rows])
    trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=ds,
                         args=SFTConfig(output_dir=a.out, num_train_epochs=a.epochs, per_device_train_batch_size=1,
                                        gradient_accumulation_steps=8, learning_rate=1e-4, logging_steps=5,
                                        bf16=True, max_seq_length=a.max_seq, dataset_text_field="text"))
    trainer.train()

    if a.dpo:
        from trl import DPOConfig, DPOTrainer
        pairs = _load_jsonl(data / "dpo_pairs.jsonl")
        dds = Dataset.from_list([{"prompt": p["prompt"] + "\n" + p.get("context", "")[-4000:],
                                  "chosen": json.dumps(p["chosen"]), "rejected": json.dumps(p["rejected"])}
                                 for p in pairs])
        DPOTrainer(model=model, ref_model=None, tokenizer=tok, train_dataset=dds,
                   args=DPOConfig(output_dir=a.out + "-dpo", per_device_train_batch_size=1,
                                  gradient_accumulation_steps=8, learning_rate=5e-6, beta=0.1)).train()
    model.save_pretrained(a.out)
    tok.save_pretrained(a.out)
    if a.gguf:
        model.save_pretrained_gguf(a.out + "-gguf", tok, quantization_method="q4_k_m")
    log.info("done: adapter in %s - serve with vLLM --enable-lora or import the GGUF into Ollama", a.out)


if __name__ == "__main__":
    main()
