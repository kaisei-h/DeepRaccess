import argparse
import csv
import time
import glob
import numpy as np
import torch.optim as optim
import torch
from Bio import SeqIO
from scipy.stats import spearmanr
from utils.bert import BertModel, get_config

import process
import mymodel

class AccDataset(torch.utils.data.Dataset):
    def __init__(self, low_seq, accessibility):
        self.data_num = len(low_seq)
        self.low_seq = low_seq
        self.accessibility = accessibility

    def __len__(self):
        return self.data_num

    def __getitem__(self, idx):
        out_low_seq = self.low_seq[idx]
        out_accessibility = self.accessibility[idx]

        return out_low_seq, out_accessibility




def make_dl(seq_data_paths, acc_data_paths, batch_size):
    max_length = 440

    data_sets = seq_data_paths
    seqs = []
    for i, data_set in enumerate(data_sets):
        for record in SeqIO.parse(data_set, "fasta"):
            record = record[::-1]  # reverse
            seq = str(record.seq).upper()
            seqs.append(seq)
    seqs_len = np.tile(np.array([len(i) for i in seqs]), 1)

    # Convert array string to index list with zero padding
    bases_list = []
    for seq in seqs:
        bases = list(seq)
        bases_list.append(bases)
    idx_dict = {"MASK": 1, "A": 2, "U": 3, "T": 3, "G": 4, "C": 5}
    low_seq = torch.tensor(np.array(process.convert(bases_list, idx_dict, max_length)))

    data_sets = acc_data_paths
    accessibility = []
    for i, data_set in enumerate(data_sets):
        with open(data_set) as f:
            reader = csv.reader(f)
            for l in reader:
                pad_acc = l + ["-1"] * (max_length - len(l))
                accessibility.append(pad_acc)
    accessibility = torch.tensor(np.array(accessibility, dtype=np.float32))

    dataset = AccDataset(low_seq, accessibility)
    train_ratio = 0.9
    train_size = int(train_ratio * len(dataset))
    val_size = len(dataset) - train_size
    train_dataset, val_dataset = torch.utils.data.random_split(
        dataset, [train_size, val_size]
    )
    train_dl = torch.utils.data.DataLoader(
        train_dataset, batch_size, num_workers=2, shuffle=True
    )
    val_dl = torch.utils.data.DataLoader(
        val_dataset, batch_size, num_workers=2, shuffle=True
    )

    return train_dl, val_dl


def train(device, model, train_dl, val_dl, criterion, optimizer, epochs, name):
    scaler = torch.cuda.amp.GradScaler()

    train_loss_list = []
    val_loss_list = []
    train_time_list = []
    val_time_list = []
    data_all = torch.tensor([])
    target_all = torch.tensor([])
    output_all = torch.tensor([])

    for epoch in range(epochs):
        print(f"Epoch {epoch+1}/{epochs}")

        for phase in ["train", "val"]:
            if device == "cuda":
                start_event = torch.cuda.Event(enable_timing=True)
                end_event = torch.cuda.Event(enable_timing=True)
                start_event.record()
            else:
                start = time.time()

            if (epoch == 0) and (phase == "train"):
                continue

            if phase == "train":
                model.train()
                epoch_loss = 0
                for batch in train_dl:
                    low_seq, accessibility = batch
                    data = low_seq.to(device, non_blocking=False)
                    target = accessibility.to(device, non_blocking=False)
                    optimizer.zero_grad()
                    if data.size()[0] == 1:
                        continue
                    with torch.cuda.amp.autocast():
                        with torch.set_grad_enabled(phase == "train"):
                            output = model(data)
                            loss = criterion(output, target)
                            scaler.scale(loss).backward()
                            scaler.step(optimizer)
                            scaler.update()
                            epoch_loss += loss.item() * data.size(0)
                avg_loss = epoch_loss / len(train_dl.dataset)

            else:
                model.eval()
                epoch_loss = 0
                for batch in val_dl:
                    low_seq, accessibility = batch
                    data = low_seq.to(device, non_blocking=False)
                    target = accessibility.to(device, non_blocking=False)
                    optimizer.zero_grad()
                    if data.size()[0] == 1:
                        continue
                    with torch.cuda.amp.autocast():
                        output = model(data)
                        if (epoch + 1) == epochs:
                            data_all = torch.cat([data_all, data.cpu().detach()])
                            target_all = torch.cat([target_all, target.cpu().detach()])
                            output_all = torch.cat([output_all, output.cpu().detach()])
                        loss = criterion(output, target)
                        epoch_loss += loss.item() * data.size(0)
                avg_loss = epoch_loss / len(val_dl.dataset)

            if device == "cuda":
                end_event.record()
                torch.cuda.synchronize()
                elapsed_time = start_event.elapsed_time(end_event) * 1000
            else:
                finish = time.time()
                elapsed_time = finish - start
            print(f"{phase} Loss:{avg_loss:.4f} Time:{elapsed_time:.4f} s")

            if phase == "val":
                val_time_list.append(elapsed_time)
                val_loss_list.append(avg_loss)
                if avg_loss < 0.01:
                    break
            elif phase == "train":
                train_time_list.append(elapsed_time)
                train_loss_list.append(avg_loss)

        torch.save(
            {
                "epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "loss": loss,
            },
            f"path/{name}.pth",
        )

    return (
        data_all,
        target_all,
        output_all,
        train_loss_list,
        val_loss_list,
        train_time_list,
        val_time_list,
    )


def main():
    print('===start training===')
    dt_now = process.get_JST_time()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    parser = argparse.ArgumentParser(description="DeepRaccess")
    parser.add_argument(
        "--seqdir",
        "-s",
        required=True,
        help="Directory of input sequences in fasta format.",
    )
    parser.add_argument(
        "--accdir",
        "-a",
        required=True,
        help="Directory of target accessibility in csv format.",
    )

    parser.add_argument(
        "--epoch", "-e", type=int, default=10, help="Number of learning epochs"
    )
    parser.add_argument("--batch", "-b", type=int, default=256, help="Batch size")
    parser.add_argument(
        "--model",
        choices=["FCN", "Unet", "BERT", "RNABERT"],
        default="FCN",
        help="Neural Network Architecture",
    )
    parser.add_argument(
        "--name",
        default=f"{dt_now}",
        help="This is the name you give to the PATH, LOG and SCATTER for this training. Default is the timestamp of the start time.",
    )

    args = parser.parse_args()

    name = args.name
    seq_paths = sorted(glob.glob(args.seqdir + "*"))
    acc_paths = sorted(glob.glob(args.accdir + "*"))

    model_type = args.model
    batch_size = args.batch
    criterion = mymodel.normMSE().to(device)

    config = get_config(file_path="utils/RNA_bert_config.json")
    if "BERT" in model_type:
        config.hidden_size = config.num_attention_heads * config.multiple
        model = BertModel(config)
        model = getattr(mymodel, "RBERT")(model)
    else:
        model = getattr(mymodel, model_type)()
    model = process.model_device(model, device)
    model = model.module.to(device)
    model.apply(mymodel.weight_init)
    if model_type == "RNABERT":
        model.load_state_dict(torch.load("path/utils_bertrna.pth"), strict=False)

    optimizer = optim.AdamW([{"params": model.parameters(), "lr": config.adam_lr}])
    epochs = args.epoch

    train_dl, val_dl = make_dl(seq_paths, acc_paths, batch_size)
    (
        data_all,
        target_all,
        output_all,
        train_loss_list,
        val_loss_list,
        train_time_list,
        val_time_list,
    ) = train(device, model, train_dl, val_dl, criterion, optimizer, epochs, name)

    target_rem, output_rem = process.remove_padding(
        torch.tensor(target_all), torch.tensor(output_all)
    )
    all_loss = (
        ((np.array(target_rem) - np.array(output_rem)) ** 2).mean(axis=0)
    ) / target_rem.mean()
    correlation, pvalue = spearmanr(
        np.array(target_rem).flatten(), np.array(output_rem).flatten()
    )
    print(f"normMSEloss{all_loss}, correlation{correlation}, pvalue{pvalue}")
    process.plot_result(
        np.array(target_rem), np.array(output_rem), mode="save", name=f"{name}.png"
    )

    with open(f"{name}.log", "w") as f:
        f.writelines(f"normMSEloss: {all_loss} \n")
        f.writelines(f"cor: {correlation} \n")
        f.writelines(f"train_time: {train_time_list} \n")
        f.writelines(f"val_time: {val_time_list} \n")
        f.writelines(f"train_loss: {train_loss_list} \n")
        f.writelines(f"val_loss: {val_loss_list} \n")
        f.writelines(f"config: {config} \n")
        f.writelines(f"criterion: {criterion} \n")
        f.writelines(f"optimizer: {optimizer} \n")
        f.writelines(f"model: {model} \n")

    print('===finish training===')

if __name__ == "__main__":
    main()
