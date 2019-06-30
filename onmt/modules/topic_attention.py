"""Global attention modules (Luong / Bahdanau)"""
import six

import torch
import torch.nn as nn
import torch.nn.functional as F

from onmt.modules.sparse_activations import sparsemax
from onmt.utils.misc import aeq, sequence_mask
from onmt.utils.logging import logger
# This class is mainly used by decoder.py for RNNs but also
# by the CNN / transformer decoder when copy attention is used
# CNN has its own attention mechanism ConvMultiStepAttention
# Transformer has its own MultiHeadedAttention


class TopicAttention(nn.Module):
    r"""
    Global attention takes a matrix and a query vector. It
    then computes a parameterized convex combination of the matrix
    based on the input query.

    Constructs a unit mapping a query `q` of size `dim`
    and a source matrix `H` of size `n x dim`, to an output
    of size `dim`.


    .. mermaid::

       graph BT
          A[Query]
          subgraph RNN
            C[H 1]
            D[H 2]
            E[H N]
          end
          F[Attn]
          G[Output]
          A --> F
          C --> F
          D --> F
          E --> F
          C -.-> G
          D -.-> G
          E -.-> G
          F --> G

    All models compute the output as
    :math:`c = \sum_{j=1}^{\text{SeqLength}} a_j H_j` where
    :math:`a_j` is the softmax of a score function.
    Then then apply a projection layer to [q, c].

    However they
    differ on how they compute the attention score.

    * Luong Attention (dot, general):
       * dot: :math:`\text{score}(H_j,q) = H_j^T q`
       * general: :math:`\text{score}(H_j, q) = H_j^T W_a q`


    * Bahdanau Attention (mlp):
       * :math:`\text{score}(H_j, q) = v_a^T \text{tanh}(W_a q + U_a h_j)`


    Args:
       dim (int): dimensionality of query and key
       coverage (bool): use coverage term
       attn_type (str): type of attention to use, options [dot,general,mlp]
       attn_func (str): attention function to use, options [softmax,sparsemax]

    """

    def __init__(self, dim, topic_dim, coverage=False, attn_type="dot",
                 attn_func="softmax"):
        super(TopicAttention, self).__init__()

        self.dim = dim
        self.topic_dim = topic_dim
        assert attn_type in ["dot", "general", "mlp"], (
            "Please select a valid attention type (got {:s}).".format(
                attn_type))
        self.attn_type = attn_type
        assert attn_func in ["softmax", "sparsemax"], (
            "Please select a valid attention function.")
        self.attn_func = attn_func

        if self.attn_type == "general":
            self.linear_in = nn.Linear(dim, dim, bias=False)
            self.linear_in_topic = nn.Linear(topic_dim, topic_dim, bias=False)
        elif self.attn_type == "mlp":
            self.linear_context = nn.Linear(dim, dim, bias=False)
            self.linear_query = nn.Linear(dim, dim, bias=True)
            self.v = nn.Linear(dim, 1, bias=False)
            self.linear_context_topic = nn.Linear(topic_dim, topic_dim, bias=False)
            self.linear_query_topic = nn.Linear(topic_dim, topic_dim, bias=True)
            self.v_topic = nn.Linear(topic_dim, 1, bias=False)
        # mlp wants it with bias
        out_bias = self.attn_type == "mlp"
        self.linear_out = nn.Linear(dim * 2, dim, bias=out_bias)

        if coverage:
            self.linear_cover = nn.Linear(1, dim, bias=False)

    def score(self, h_t, h_s):
        """
        Args:
          h_t (FloatTensor): sequence of queries ``(batch, tgt_len, dim)``
          h_s (FloatTensor): sequence of sources ``(batch, src_len, dim``

        Returns:
          FloatTensor: raw attention scores (unnormalized) for each src index
            ``(batch, tgt_len, src_len)``
        """

        # Check input sizes
        src_batch, src_len, src_dim = h_s.size()
        tgt_batch, tgt_len, tgt_dim = h_t.size()
        aeq(src_batch, tgt_batch)
        aeq(src_dim, tgt_dim)

        if self.attn_type in ["general", "dot"]:
            if self.attn_type == "general":
                h_t_ = h_t.view(tgt_batch * tgt_len, tgt_dim)
                h_t_ = self.linear_in(h_t_)
                h_t = h_t_.view(tgt_batch, tgt_len, tgt_dim)
            h_s_ = h_s.transpose(1, 2)
            # (batch, t_len, d) x (batch, d, s_len) --> (batch, t_len, s_len)
            return torch.bmm(h_t, h_s_)
        else:
            dim = self.dim
            wq = self.linear_query(h_t.view(-1, dim))
            wq = wq.view(tgt_batch, tgt_len, 1, dim)
            wq = wq.expand(tgt_batch, tgt_len, src_len, dim)

            uh = self.linear_context(h_s.contiguous().view(-1, dim))
            uh = uh.view(src_batch, 1, src_len, dim)
            uh = uh.expand(src_batch, tgt_len, src_len, dim)

            # (batch, t_len, s_len, d)
            wquh = torch.tanh(wq + uh)

            return self.v(wquh.view(-1, dim)).view(tgt_batch, tgt_len, src_len)

    def score_topic(self, h_t, h_s):
        """
        Args:
          h_t (FloatTensor): sequence of queries ``(batch, tgt_len, dim)``
          h_s (FloatTensor): sequence of sources ``(batch, src_len, dim``

        Returns:
          FloatTensor: raw attention scores (unnormalized) for each src index
            ``(batch, tgt_len, src_len)``
        """

        # Check input sizes
        src_batch, src_len, src_dim = h_s.size()
        tgt_batch, tgt_len, tgt_dim = h_t.size()
        aeq(src_batch, tgt_batch)
        aeq(src_dim, tgt_dim)
        h_s_ = h_s.transpose(1, 2)
        # Adding parameter (10e4) for squashing small number
        result = torch.bmm(h_t, h_s_) * 10e4
        # Minux the max for numerical stability
        return result - torch.max(result)
        # (batch, t_len, d) x (batch, d, s_len) --> (batch, t_len, s_len)
        # if self.attn_type in ["general", "dot"]:
        #     if self.attn_type == "general":
        #         h_t_ = h_t.view(tgt_batch * tgt_len, tgt_dim)
        #         h_t_ = self.linear_in_topic(h_t_)
        #         h_t = h_t_.view(tgt_batch, tgt_len, tgt_dim)
        #     h_s_ = h_s.transpose(1, 2)
        #     # (batch, t_len, d) x (batch, d, s_len) --> (batch, t_len, s_len)
        #     return torch.bmm(h_t, h_s_)
        # else:
        #     dim = self.topic_dim
        #     wq = self.linear_query_topic(h_t.view(-1, dim))
        #     wq = wq.view(tgt_batch, tgt_len, 1, dim)
        #     wq = wq.expand(tgt_batch, tgt_len, src_len, dim)
        #
        #     uh = self.linear_context_topic(h_s.contiguous().view(-1, dim))
        #     uh = uh.view(src_batch, 1, src_len, dim)
        #     uh = uh.expand(src_batch, tgt_len, src_len, dim)
        #
        #     # (batch, t_len, s_len, d)
        #     wquh = torch.tanh(wq + uh)
        #
        #     return self.v_topic(wquh.view(-1, dim)).view(tgt_batch, tgt_len, src_len)

    def mix_probs(self, std, topic, theta):
        mixture = torch.log(std) + theta * torch.log(topic/std + 1)
        return mixture.exp()

    def forward(self, source, memory_bank, source_topic, topic_bank, unk_topic, theta,
                memory_lengths=None, coverage=None, sample=None, fusion=None):
        """

        Args:
          source (FloatTensor): query vectors ``(batch, tgt_len, dim)``
          source_topic (FloatTensor): query topic vectors ``(batch, tgt_len, dim)``
          memory_bank (FloatTensor): source vectors ``(batch, src_len, dim)``
          topic_bank (FloatTensor): source topic vectors ``(batch, src_len, dim)``
          memory_lengths (LongTensor): the source context lengths ``(batch,)``
          coverage (FloatTensor): None (not supported yet)

        Returns:
          (FloatTensor, FloatTensor):

          * Computed vector ``(tgt_len, batch, dim)``
          * Attention distributions for each query
            ``(tgt_len, batch, src_len)``
        """

        # one step input
        if source.dim() == 2:
            one_step = True
            source = source.unsqueeze(1)
        else:
            one_step = False

        batch, source_l, dim = memory_bank.size()
        batch_, target_l, dim_ = source.size()
        aeq(batch, batch_)
        aeq(dim, dim_)
        aeq(self.dim, dim)

        if coverage is not None:
            batch_, source_l_ = coverage.size()
            aeq(batch, batch_)
            aeq(source_l, source_l_)

        if coverage is not None:
            cover = coverage.view(-1).unsqueeze(1)
            memory_bank += self.linear_cover(cover).view_as(memory_bank)
            memory_bank = torch.tanh(memory_bank)

        ## Global alignment
        # compute attention scores, as in Luong et al.
        align = self.score(source, memory_bank)

        if memory_lengths is not None:
            mask = sequence_mask(memory_lengths, max_len=align.size(-1))
            mask = mask.unsqueeze(1)  # Make it broadcastable.
            align.masked_fill_(1 - mask, -float('inf'))

        # Softmax or sparsemax to normalize attention weights
        if self.attn_func == "softmax":
            align_vectors = F.softmax(align.view(batch*target_l, source_l), -1)
        else:
            align_vectors = sparsemax(align.view(batch*target_l, source_l), -1)
        align_vectors = align_vectors.view(batch, target_l, source_l)
        if theta == 1.0:
            mixture_align_vectors = align_vectors
            topic_align_vectors = align_vectors
        else:
            ## Topic alignment
            # Scaling by 10e4 to prevent buffer underflow
            topic_align = self.score_topic(source_topic, topic_bank)
            if memory_lengths is not None:
                mask = sequence_mask(memory_lengths, max_len=topic_align.size(-1))
                mask = mask.unsqueeze(1)  # Make it broadcastable.
                topic_align.masked_fill_(1 - mask, -float('inf'))
                if self.attn_func == "softmax":
                    topic_align_vectors = F.softmax(
                        topic_align.view(batch * target_l, source_l), -1)
                else:
                    topic_align_vectors = sparsemax(topic_align.view(batch * target_l, source_l), -1)
                topic_align_vectors = topic_align_vectors.view(batch, target_l, source_l)

                mixture_align_vectors = self.mix_probs(align_vectors, topic_align_vectors, theta)
                # Replace unk_topic with standard attention
                # unk_idx = [1 if torch.eq(row, unk_topic).all() else 0 for row in source_topic]
                # for idx, value in enumerate(unk_idx):
                #      if value == 1:
                #          mixture_align_vectors.data[idx] = align_vectors.data[idx]
                #          topic_align_vectors.data[idx] = align_vectors.data[idx]
        # each context vector c_t is the weighted average
        # over all the source hidden states
        if theta == 1.0:
            c = torch.bmm(align_vectors, memory_bank)
        else:
            c = torch.bmm(mixture_align_vectors, memory_bank)

        # concatenate
        concat_c = torch.cat([c, source], 2).view(batch*target_l, dim*2)
        attn_h = self.linear_out(concat_c).view(batch, target_l, dim)
        if self.attn_type in ["general", "dot"]:
            attn_h = torch.tanh(attn_h)

        if one_step:
            attn_h = attn_h.squeeze(1)
            align_vectors = align_vectors.squeeze(1)
            mixture_align_vectors = mixture_align_vectors.squeeze(1)
            topic_align_vectors = topic_align_vectors.squeeze(1)

            # Check output sizes
            batch_, dim_ = attn_h.size()
            aeq(batch, batch_)
            aeq(dim, dim_)
            batch_, source_l_ = align_vectors.size()
            aeq(batch, batch_)
            aeq(source_l, source_l_)

        else:
            attn_h = attn_h.transpose(0, 1).contiguous()
            align_vectors = align_vectors.transpose(0, 1).contiguous()
            topic_align_vectors = topic_align_vectors.transpose(0, 1).contiguous()
            mixture_align_vectors = mixture_align_vectors.transpose(0, 1).contiguous()
            # Check output sizes
            target_l_, batch_, dim_ = attn_h.size()
            aeq(target_l, target_l_)
            aeq(batch, batch_)
            aeq(dim, dim_)
            target_l_, batch_, source_l_ = align_vectors.size()
            aeq(target_l, target_l_)
            aeq(batch, batch_)
            aeq(source_l, source_l_)
        return attn_h, align_vectors, topic_align_vectors, mixture_align_vectors
