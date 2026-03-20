"""
Pre-download and cache a Hugging Face model for Muscle.

Usage:
  python download_model.py
  python download_model.py --model-id NousResearch/Hermes-2.5-Mistral-7B --dtype float16
  python download_model.py --model-id NousResearch/Hermes-2.5-Mistral-7B --quantize 8bit
"""

import argparse
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download/cache HF model for Muscle")
    parser.add_argument(
        "--model-id",
        default="NousResearch/Hermes-2.5-Mistral-7B",
        help="Hugging Face model ID",
    )
    parser.add_argument(
        "--dtype",
        choices=["float16", "float32"],
        default="float16",
        help="Model dtype when not quantized",
    )
    parser.add_argument(
        "--quantize",
        choices=["none", "8bit"],
        default="none",
        help="Optional quantization mode",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print(f"Downloading tokenizer: {args.model_id}")
    AutoTokenizer.from_pretrained(args.model_id)

    if args.quantize == "8bit":
        print("Loading 8-bit quantized model")
        quant_cfg = BitsAndBytesConfig(load_in_8bit=True)
        AutoModelForCausalLM.from_pretrained(
            args.model_id,
            quantization_config=quant_cfg,
            device_map="auto",
        )
    else:
        dtype = torch.float16 if args.dtype == "float16" else torch.float32
        print(f"Loading model with dtype={args.dtype}")
        AutoModelForCausalLM.from_pretrained(
            args.model_id,
            torch_dtype=dtype,
            device_map="auto",
        )

    print("Model cached successfully.")


if __name__ == "__main__":
    main()
