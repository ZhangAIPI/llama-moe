import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import torch
from datasets import load_dataset
from sklearn.metrics import accuracy_score
from tqdm import tqdm
from transformers import LlamaTokenizer

from smoe.models.llama_moefication.modeling_llama_moe import LlamaMoEForCausalLM
from smoe.utils.eval.crop import crop
from smoe.utils.eval.gather_results import gather_results

choices = ["A", "B", "C", "D"]


def softmax(x):
    z = x - max(x)
    numerator = np.exp(z)
    denominator = np.sum(numerator)
    softmax = numerator / denominator
    return softmax


def format_subject(subject):
    l = subject.split("_")
    s = ""
    for entry in l:
        s += " " + entry
    return s


def format_example(df, idx, include_answer=True):
    prompt = df.iloc[idx, 0]
    k = df.shape[1] - 2
    for j in range(k):
        prompt += "\n{}. {}".format(choices[j], df.iloc[idx, j + 1])
    prompt += "\nAnswer:"
    if include_answer:
        prompt += " {}\n\n".format(df.iloc[idx, k + 1])
    return prompt


def gen_prompt(train_df, subject, k=-1):
    prompt = "The following are multiple choice questions (with answers) about {}.\n\n".format(
        format_subject(subject)
    )
    if k == -1:
        k = train_df.shape[0]
    for i in range(k):
        prompt += format_example(train_df, i)
    return prompt


def eval(args, tokenizer, model, subject, dev_df, test_df):
    cors = []
    all_probs = []
    answers = choices[: test_df.shape[1] - 2]

    model.eval()
    torch.cuda.empty_cache()
    with torch.no_grad():
        for i in tqdm(range(test_df.shape[0]), desc=f"Processing {subject}"):
            # get prompt and make sure it fits
            k = args.ntrain
            prompt_end = format_example(test_df, i, include_answer=False)
            train_prompt = gen_prompt(dev_df, subject, k)
            prompt = train_prompt + prompt_end

            while crop(prompt) != prompt:
                k -= 1
                train_prompt = gen_prompt(dev_df, subject, k)
                prompt = train_prompt + prompt_end

            label = test_df.iloc[i, test_df.shape[1] - 1]

            scores = []
            for choice in choices:
                input_text = prompt + "\n" + choice
                inputs = tokenizer(input_text, return_tensors="pt")
                inputs = {k: v.to(model.device) for k, v in inputs.items()}
                outputs = model(**inputs)
                scores.append(outputs.logits[0, -1, :].max().item())
            pred = {0: "A", 1: "B", 2: "C", 3: "D"}[np.argmax(scores)]
            probs = softmax(np.array(scores))

            cor = pred == label
            cors.append(cor)
            all_probs.append(probs)

        acc = np.mean(cors)
        cors = np.array(cors)

        all_probs = np.array(all_probs)
        print("Average accuracy {:.3f} - {}".format(acc, subject))

    return cors, acc, all_probs


def main(args):
    subjects = sorted(
        [
            f.split("_test.csv")[0]
            for f in os.listdir(os.path.join(args.data_dir, "test"))
            if "_test.csv" in f
        ]
    )
    subjects = subjects[44:48] + subjects[49:]
    try:
        if not os.path.exists(args.save_dir):
            os.makedirs(args.save_dir)
    except:
        print("args.save_dir has been made!")
    if os.path.isfile(os.path.join(args.save_dir, "all_datasets_2.txt")):
        os.remove(os.path.join(args.save_dir, "all_datasets_2.txt"))

    print(subjects, "\n")
    print(args, "\n")

    tokenizer = LlamaTokenizer.from_pretrained(args.tokenizer_path)
    model = LlamaMoEForCausalLM.from_pretrained(args.model_path)
    tokenizer.pad_token = tokenizer.eos_token
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)

    if args.select_num is not None:
        model.set_moe_num_selects(args.select_num)

    all_cors = []

    for subject in subjects:
        dev_df = pd.read_csv(
            os.path.join(args.data_dir, "dev", subject + "_dev.csv"), header=None
        )[: args.ntrain]
        test_df = pd.read_csv(
            os.path.join(args.data_dir, "test", subject + "_test.csv"), header=None
        )

        cors, acc, probs = eval(args, tokenizer, model, subject, dev_df, test_df)

        with open(os.path.join(args.save_dir, "all_datasets_2.txt"), "a+") as file:
            file.write("Average accuracy {:.3f} - {}\n".format(acc, subject))

        all_cors.append(cors)

        test_df["correct"] = cors
        for j in range(probs.shape[1]):
            choice = choices[j]
            test_df["choice{}_probs".format(choice)] = probs[:, j]
        test_df.to_csv(
            os.path.join(args.save_dir, "{}.csv".format(subject)), index=None
        )

    weighted_acc = np.mean(np.concatenate(all_cors))
    print("Average accuracy: {:.3f}".format(weighted_acc))
    gather_results(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ntrain", "-k", type=int, default=5)
    parser.add_argument("--data_dir", "-d", type=str, default="data")
    parser.add_argument("--save_dir", "-s", type=str, default="results_moe")
    parser.add_argument("--tokenizer_path", type=str)
    parser.add_argument("--model_path", type=str)
    parser.add_argument("--select_num", type=int)
    args = parser.parse_args()
    main(args)
