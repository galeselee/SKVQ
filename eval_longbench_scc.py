import os
import torch
import json
import numpy as np
import random
import argparse

from datasets import load_dataset, load_from_disk
from tqdm import tqdm
from transformers import AutoTokenizer, LlamaTokenizer, AutoModelForCausalLM

from calib_config import *
from KVcache_manager import ModelKVCacheManager
from experiments.modeling_llama_skvq import LlamaForCausalLM
from experiments.modeling_mistral_skvq import MistralForCausalLM
from experiments.utils import plug_quantizer_into_model


def parse_args(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_name', type=str, default=None, choices=["mistral-7b-instruct", "llama3-instruct"])
    parser.add_argument('--quant', default=None)
    return parser.parse_args(args)

# # This is the customized building prompt for chat models
# def build_chat(tokenizer, prompt, model_name):
#     if "chatglm3" in model_name:
#         prompt = tokenizer.build_chat_input(prompt)
#     elif "chatglm" in model_name:
#         prompt = tokenizer.build_prompt(prompt)
#     elif "longchat" in model_name or "vicuna" in model_name:
#         from fastchat.model import get_conversation_template
#         conv = get_conversation_template("vicuna")
#         conv.append_message(conv.roles[0], prompt)
#         conv.append_message(conv.roles[1], None)
#         prompt = conv.get_prompt()
#     elif "llama2-7b-80k" in model_name:
#         prompt = f"<|im_start|> {prompt}"
#     elif "llama2" in model_name:
#         prompt = f"[INST]{prompt}[/INST]"
#     elif "mistral" in model_name and "instruct" in model_name:
#         prompt = f"[INST]{prompt}[/INST]"
#     elif "xgen" in model_name:
#         header = (
#             "A chat between a curious human and an artificial intelligence assistant. "
#             "The assistant gives helpful, detailed, and polite answers to the human's questions.\n\n"
#         )
#         prompt = header + f" ### Human: {prompt}\n###"
#     elif "internlm" in model_name:
#         prompt = f"<|User|>:{prompt}<eoh>\n<|Bot|>:"
#     return prompt

def build_chat(tokenizer, prompt, model_name):
    if "llama" in model_name.lower():
        messages = [
            {"role": "user", "content": prompt},
        ]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        print("i'm here")
    elif "mistral" in model_name.lower():
        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return prompt


def post_process(response, model_name):
    if "xgen" in model_name:
        response = response.strip().replace("Assistant:", "")
    elif "internlm" in model_name:
        response = response.split("<eoa>")[0]
    return response


@torch.no_grad()
def get_pred(model_name: str, model: LlamaForCausalLM, tokenizer: LlamaTokenizer, data, max_length, max_gen, prompt_format, dataset:str, out_path, **kwargs):
    device = model.device
    for data_idx, json_obj in enumerate(tqdm(data)):
        # if data_idx != 4:
        #     continue

        prompt = prompt_format.format(**json_obj)
        # truncate to fit max_length (we suggest truncate in the middle, since the left and right side may contain crucial instructions)
        tokenized_prompt = tokenizer(prompt, truncation=False, return_tensors="pt").input_ids[0]
        if "chatglm3" in model_name:
            tokenized_prompt = tokenizer(prompt, truncation=False, return_tensors="pt", add_special_tokens=False).input_ids[0]
        if len(tokenized_prompt) > max_length:
            half = int(max_length/2)
            prompt = tokenizer.decode(tokenized_prompt[:half], skip_special_tokens=True)+tokenizer.decode(tokenized_prompt[-half:], skip_special_tokens=True)
        if dataset not in ["trec", "triviaqa", "samsum", "lsht", "lcc", "repobench-p"]: # chat models are better off without build prompts on these tasks
            prompt = build_chat(tokenizer, prompt, model_name)
        if "chatglm3" in model_name:
            input = prompt.to(device)
        else:
            input = tokenizer(prompt, truncation=False, return_tensors="pt").to(device)
        context_length = input.input_ids.shape[-1]
        try:
            if dataset == "samsum": # prevent illegal output on samsum (model endlessly repeat "\nDialogue"), might be a prompting issue
                output = model.generate(
                    **input,
                    max_new_tokens=max_gen,
                    num_beams=1,
                    do_sample=False,
                    temperature=0.0,
                    min_length=context_length+1,
                    eos_token_id=[tokenizer.eos_token_id, tokenizer.encode("\n", add_special_tokens=False)[-1]],
                    pad_token_id=tokenizer.eos_token_id,
                )[0]
            else:
                output = model.generate(
                    **input,
                    max_new_tokens=max_gen,
                    num_beams=1,
                    do_sample=False,
                    temperature=0.0,
                    pad_token_id=tokenizer.eos_token_id,
                )[0]
            if model.model_kv_manager is not None:
                model.model_kv_manager.clear()

            pred = tokenizer.decode(output[context_length:], skip_special_tokens=True)
            pred = post_process(pred, model_name)
            with open(out_path, "a", encoding="utf-8") as f:
                json.dump({"pred": pred, "answers": json_obj["answers"], "all_classes": json_obj["all_classes"], "length": json_obj["length"]}, f, ensure_ascii=False)
                f.write('\n')
        except torch.cuda.OutOfMemoryError as e:
            print(f"{dataset} {data_idx} out of memory")
            torch.cuda.empty_cache()
            
        torch.cuda.empty_cache()
    # dist.destroy_process_group()


def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.cuda.manual_seed_all(seed)

def get_quantizer_from_str(
    s: str, model, model_name: str
) -> ModelKVCacheManager:
    """
    example: "k2-v2-g128-w128-reoder-pre_rope"
             "k2-v2-g128-w128-reoder-clip-pre_rope"
             "k2-v2-g128-w128-reoder-clip-sink-pre_rope"
             "k2-v2-g128-w128-smooth-pre_rope"
             "k2-v2-g128-w128-smooth"
             "k2-v2-g128-w128"
             "k2-v2-g128-KIVI"
             "k2-v2-g128-rptq"
             "k2-v2-g128-smoothquant"
             "k2-v2-g128-rtn"
    """
    if s is None or s.strip().lower() == "none":
        return None

    opts = s.strip().split("-")
    kbits = float(opts[0][1:])
    vbits = float(opts[1][1:])
    kbits = int(kbits) if kbits == round(kbits) else kbits
    vbits = int(vbits) if vbits == round(vbits) else vbits
    gsize = int(opts[2][1:])
    if ("rtn" in s) or ("rptq" in s) or ("smoothquant" in s):
        window = 0
    else:
        window = int(opts[3][1:])

    smooth_file = MODEL_TO_SMOOTH[model_name] if "smooth" in s else None
    reorder_file = (
        MODEL_TO_REORDER[model_name][gsize]["minmax"]
        if (("reorder" in s) or ("rod" in s) or ("rptq" in s))
        else None
    )

    pre_rope = (
        True
        if ("pre_rope" in s) or ("rptq" in s) or ("smoothquant" in s)
        else False
    )

    clipping = [(0.92 if "clip" in s else 1.0) for _ in range(len(model.model.layers))]
    full_prefill = False if ("rtn" in s) or ("rptq" in s) or ("smoothquant" in s) else True
    KIVI_mode = "KIVI" in s
    fp8 = "fp8" in s
    sink = int(s.split("sink")[1].split("-")[0]) if "sink" in s else 0

    quantizer = ModelKVCacheManager.create(
        model=model,
        kbits=kbits,
        vbits=vbits,
        gsize=gsize,
        window_size=window,
        reorder_file=reorder_file,
        smooth_file=smooth_file,
        clipping=clipping,
        pre_rope=pre_rope,
        full_prefill=full_prefill,
        KIVI_mode=KIVI_mode,
        fp8=fp8,
        attn_sink=sink,
    )
    print(f"{'='*30}ModelKVManager{'='*30}\n{quantizer}")
    return quantizer


def load_model_custom(model_name: str, model_path: str, quant_scheme: str):

    tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False, trust_remote_code=True)
    if "mistral" in model_name:
        model = MistralForCausalLM.from_pretrained(
            model_path,
            device_map="auto",
            torch_dtype=torch.float16,
            use_flash_attention_2=True,
        )
    else:
        model = LlamaForCausalLM.from_pretrained(
            model_path,
            device_map="auto",
            torch_dtype=torch.float16,
            use_flash_attention_2=True,
        )

    fake_quantizer:ModelKVCacheManager = get_quantizer_from_str(quant_scheme, model=model, model_name=model_name)
    plug_quantizer_into_model(
        model,
        fake_quantizer
    )
    return model, tokenizer, fake_quantizer



if __name__ == '__main__':
    seed_everything(42)
    args = parse_args()
    world_size = torch.cuda.device_count()
    # PROJ_DIR = os.path.dirname(__file__)
    model2path = json.load(open(f"./longbench_config/model2path.json", "r"))
    model2maxlen = json.load(open(f"./longbench_config/model2maxlen.json", "r"))
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    model_name = args.model_name
    quant_scheme = args.quant

    # define your model
    max_length = model2maxlen[model_name]
    model_path = model2path[model_name]

    # get quantizer
    # if model_name == "llama2-7b-chat-4k":
    #     model_name = "llama2-7b-chat"
    # elif model_name == "llama2-13b-chat-4k":
    #     model_name = "llama2-13b-chat"
    # elif model_name == "mistral-7b-instruct":
    #     model_name = "mistral-7b-instruct-v0.2"
    # else:
    #     model_name = model_name

    # fake_quantizer = get_quantizer_from_str(quant_scheme, name)

    datasets = ["qasper", "multifieldqa_en", "hotpotqa", "2wikimqa", "gov_report", "multi_news", \
            "trec", "triviaqa", "samsum", "passage_count", "passage_retrieval_en", "lcc", "repobench-p"]
        # datasets = ["qasper", "multifieldqa_en", "hotpotqa", "2wikimqa", "gov_report", "multi_news", \
        #     "trec", "triviaqa", "samsum", "passage_count", "passage_retrieval_en"]
        # datasets = ["hotpotqa"]
        # datasets = ["narrativeqa", "qasper", "multifieldqa_en", "multifieldqa_zh", "hotpotqa", "2wikimqa", "musique", \
        #             "dureader", "gov_report", "qmsum", "multi_news", "vcsum", "trec", "triviaqa", "samsum", "lsht", \
        #             "passage_count", "passage_retrieval_en", "passage_retrieval_zh", "lcc", "repobench-p"]

        # datasets = [
        #     "multifieldqa_en", "2wikimqa", "passage_retrieval_en", "lcc", "gov_report",
        #     "qasper", "qmsum", "multi_news", "trec", "triviaqa", "samsum", "repobench-p",
        #     "multifieldqa_zh", "hotpotqa", "musique", "dureader", "lsht",
        #     # "narrativeqa", "vcsum", "passage_count", "passage_retrieval_zh",
        # ]

    # we design specific prompt format and max generation length for each task, feel free to modify them to optimize model output
    dataset2prompt = json.load(open(f"{PROJ_DIR}/longbench_config/dataset2prompt.json", "r"))
    dataset2maxlen = json.load(open(f"{PROJ_DIR}/longbench_config/dataset2maxlen.json", "r"))
    # predict on each dataset
    print(f"* Datasets: {datasets}")
    model, tokenizer, fake_quantizer = load_model_custom(model_name, model_path, quant_scheme=quant_scheme)

    quant_tag = f"-{fake_quantizer.tag()}" if (quant_scheme and quant_scheme != "None") else ""
    for dataset in datasets:
        data = load_from_disk(f"/data/user/user93/data/longbench_local/{dataset}")
        if not os.path.exists(f"pred_e/{model_name}{quant_tag}"):
            os.makedirs(f"pred_e/{model_name}{quant_tag}")
        out_path = f"pred_e/{model_name}{quant_tag}/{dataset}.jsonl"
        prompt_format = dataset2prompt[dataset]
        max_gen = dataset2maxlen[dataset]

        get_pred(model_name, model, tokenizer, data, max_length, max_gen, prompt_format, dataset, out_path)
