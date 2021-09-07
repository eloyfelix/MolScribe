import torch
from bms.inference.decode_strategy import DecodeStrategy


def sample_with_temperature(logits, sampling_temp, keep_topk):
    """Select next tokens randomly from the top k possible next tokens.

    Samples from a categorical distribution over the ``keep_topk`` words using
    the category probabilities ``logits / sampling_temp``.
    """

    if sampling_temp == 0.0 or keep_topk == 1:
        # argmax
        topk_scores, topk_ids = logits.topk(1, dim=-1)
        if sampling_temp > 0:
            topk_scores /= sampling_temp
    else:
        logits = torch.div(logits, sampling_temp)
        if keep_topk > 0:
            top_values, top_indices = torch.topk(logits, keep_topk, dim=1)
            kth_best = top_values[:, -1].view([-1, 1])
            kth_best = kth_best.repeat([1, logits.shape[1]]).float()
            ignore = torch.lt(logits, kth_best)
            logits = logits.masked_fill(ignore, -10000)

        dist = torch.distributions.Multinomial(logits=logits, total_count=1)
        topk_ids = torch.argmax(dist.sample(), dim=1, keepdim=True)
        topk_scores = logits.gather(dim=1, index=topk_ids)

    return topk_ids, topk_scores


class GreedySearch(DecodeStrategy):
    """Select next tokens randomly from the top k possible next tokens.
    """

    def __init__(self, pad, bos, eos, batch_size, min_length,
                 return_attention, max_length, sampling_temp=1, keep_topk=1):
        super().__init__(
            pad, bos, eos, batch_size, 1, min_length, return_attention, max_length)
        self.sampling_temp = sampling_temp
        self.keep_topk = keep_topk
        self.topk_scores = None

    def initialize(self, memory_bank, device=None):
        fn_map_state = None

        if device is None:
            device = memory_bank.device

        self.memory_length = memory_bank.size(1)
        super().initialize(memory_bank, device)

        self.select_indices = torch.arange(
            self.batch_size, dtype=torch.long, device=device)
        self.original_batch_idx = torch.arange(
            self.batch_size, dtype=torch.long, device=device)

        return fn_map_state, memory_bank

    @property
    def current_predictions(self):
        return self.alive_seq[:, -1]

    @property
    def batch_offset(self):
        return self.select_indices

    def _pick(self, log_probs):
        """Function used to pick next tokens.
        """
        topk_ids, topk_scores = sample_with_temperature(
            log_probs, self.sampling_temp, self.keep_topk)
        return topk_ids, topk_scores

    def advance(self, log_probs, attn):
        """Select next tokens randomly from the top k possible next tokens.
        """
        self.ensure_min_length(log_probs)
        topk_ids, self.topk_scores = self._pick(log_probs)
        self.is_finished = topk_ids.eq(self.eos)
        self.alive_seq = torch.cat([self.alive_seq, topk_ids], -1)

        if self.return_attention:
            if self.alive_attn is None:
                self.alive_attn = attn
            else:
                self.alive_attn = torch.cat([self.alive_attn, attn], 0)
        self.ensure_max_length()

    def update_finished(self):
        """Finalize scores and predictions."""
        finished_batches = self.is_finished.view(-1).nonzero()
        for b in finished_batches.view(-1):
            b_orig = self.original_batch_idx[b]
            # scores/predictions/attention are lists,
            # (to be compatible with beam-search)
            self.scores[b_orig].append(self.topk_scores[b, 0].item())
            self.predictions[b_orig].append(self.alive_seq[b, 1:])
            self.attention[b_orig].append(
                self.alive_attn[:, b, :self.memory_length]
                if self.alive_attn is not None else [])
        self.done = self.is_finished.all()
        if self.done:
            return
        is_alive = ~self.is_finished.view(-1)
        self.alive_seq = self.alive_seq[is_alive]
        if self.alive_attn is not None:
            self.alive_attn = self.alive_attn[:, is_alive]
        self.select_indices = is_alive.nonzero().view(-1)
        self.original_batch_idx = self.original_batch_idx[is_alive]
        # select_indices is equal to original_batch_idx for greedy search?