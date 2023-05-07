import argparse
import numpy as np
import torch
import more_itertools
from Bio import SeqIO
from utils.bert import BertModel, get_config

import result
import mymodel


def model_device(model, device):
    print("device: ", device)
    model.to(device)
    model = torch.nn.DataParallel(model, device_ids=[0, 1, 2, 3])  # make parallel
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    return model


class AccDataset(torch.utils.data.Dataset):
    def __init__(self, low_seq):
        self.data_num = len(low_seq)
        self.low_seq = low_seq

    def __len__(self):
        return self.data_num

    def __getitem__(self, idx):
        out_low_seq = self.low_seq[idx]

        return out_low_seq


def convert(seqs, kmer_dict, max_length):
    # 文字列リストを数字に変換
    seq_idx = []
    if not max_length:
        max_length = max([len(i) for i in seqs])
    for s in seqs:
        # AUTGC以外の不確定塩基はMASK
        convered_seq = [kmer_dict[i] if i in kmer_dict.keys() else 1 for i in s] + [0] * (max_length - len(s))
        seq_idx.append(convered_seq)
    return seq_idx


def make_dl(seq_data_path, batch_size):
    flag = False
    division = 1
    max_length = 440

    seq_data_path
    seqs = []
    for record in SeqIO.parse(seq_data_path, "fasta"):
        record = record[::-1]  # reverse
        seq = str(record.seq).upper()
        seqs.append(seq)
    seqs_len = np.tile(np.array([len(i) for i in seqs]), 1)

    if max(seqs_len) > max_length:
        flag = True
        division += (max(seqs_len) - 110) // 330
        max_length += division * 330

    # 配列文字列をindexリストに変換してゼロpadding
    bases_list = []
    for seq in seqs:
        bases = list(seq)
        bases_list.append(bases)
    idx_dict = {"MASK": 1, "A": 2, "U": 3, "T": 3, "G": 4, "C": 5}
    low_seq = torch.tensor(np.array(convert(bases_list, idx_dict, max_length)))

    if flag:  # window処理
        splited_seq = []
        for i in low_seq:
            splited_seq.append(list(more_itertools.windowed(i, 440, step=330)))
        low_seq = torch.tensor(splited_seq)
        num_seq, division, length = low_seq.shape
        low_seq = low_seq.view(-1, length)

    ds_ACC = AccDataset(low_seq)
    dl_ACC = torch.utils.data.DataLoader(
        ds_ACC, batch_size, num_workers=2, shuffle=False
    )

    return dl_ACC, flag, division


def windowed(output, flag, division):
    # 長い配列を復元する
    if flag:
        for i in range(division):
            if i == 0:
                low_out = output[i::division, :-55]
            elif i == division - 1:
                output = np.concatenate([low_out, output[i::division, 55:]], axis=1)
            else:
                low_out = np.concatenate([low_out, output[i::division, 55:-55]], axis=1)
        return output

    else:
        return output


def predict(device, model, dataloader):
    model.to(device)

    data_all = []
    output_all = []
    model.eval()

    with torch.no_grad():
        for batch in dataloader:
            low_seq = batch
            data = low_seq.to(device, non_blocking=False)
            output = model(data)

            data_all.append(data.cpu().detach().numpy())
            output_all.append(output.cpu().detach().numpy())

    data_all = np.concatenate(data_all)
    output_all = np.concatenate(output_all)

    return data_all, output_all


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    parser = argparse.ArgumentParser(description="DeepRaccess")
    parser.add_argument(
        "--seqfile", "-s", required=True, help="Input sequences in fasta format."
    )
    parser.add_argument(
        "--outfile", "-o", required=True, help="File name for Output accessibility"
    )

    parser.add_argument("--batch", "-b", type=int, default=256, help="Batch size")
    parser.add_argument(
        "--pretrain",
        "-p",
        default="path/FCN_structured.pth",
        help="Path of pretrained weight",
    )
    parser.add_argument(
        "--model",
        choices=["FCN", "Unet", "BERT", "RNABERT"],
        default="FCN",
        help="Neural Network Architecture",
    )

    args = parser.parse_args()

    seq_path = args.seqfile

    model_type = args.model
    batch_size = args.batch
    if "BERT" in model_type:
        config = get_config(file_path="utils/RNA_bert_config.json")
        config.hidden_size = config.num_attention_heads * config.multiple
        model = BertModel(config)
        model = getattr(mymodel, "RBERT")(model)
    else:
        model = getattr(mymodel, model_type)()
    model = model_device(model, device)
    model.load_state_dict(
        torch.load(args.pretrain, map_location=device)["model_state_dict"]
    )
    model = model.module.to(device)

    dl, flag, division = make_dl(seq_path, batch_size)
    data, output = predict(device, model, dl)
    output = windowed(output, flag, division)
    np.savetxt(args.outfile, output, delimiter=",")


if __name__ == "__main__":
    main()
