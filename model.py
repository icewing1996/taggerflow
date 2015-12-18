import collections

import numpy as np
import tensorflow as tf

from tensorflow.python.ops import rnn
from tensorflow.python.ops import rnn_cell
from tensorflow.python.ops import seq2seq

import features

class SupertaggerModel(object):

    def __init__(self, config):
        self.config = config

        # Redeclare some configuration settings for convenience.
        batch_size = config.batch_size
        supertags_size = config.supertag_space.size()
        embedding_spaces = config.embedding_spaces
        max_tokens = config.max_tokens

        with tf.name_scope("inputs"):
            # Each training step is batched with a maximum length.
            self.x = tf.placeholder(tf.int32, [batch_size, max_tokens, len(embedding_spaces)], name="x")
            self.y = tf.placeholder(tf.int32, [batch_size, max_tokens], name="y")
            self.num_tokens = tf.placeholder(tf.int64, [batch_size], name="num_tokens")
            self.keep_probability = tf.placeholder(tf.float32, [], name="keep_probability")

        # From feature indexes to concatenated embeddings.
        with tf.name_scope("embeddings"), tf.device("/cpu:0"):
            self.embeddings_w = collections.OrderedDict((name, tf.get_variable("{}_embedding_w".format(name), [space.size(), space.embedding_size])) for name, space in embedding_spaces.items() )
            embeddings = [tf.squeeze(tf.nn.embedding_lookup(e,i), [2]) for e,i in zip(self.embeddings_w.values(), tf.split(2, len(embedding_spaces), self.x))]
            concat_embedding = tf.concat(2, embeddings)
            concat_embedding = tf.nn.dropout(concat_embedding, self.keep_probability)

        with tf.name_scope("lstm"):
            # Split into LSTM inputs.
            inputs = tf.split(1, max_tokens, concat_embedding)
            inputs = [tf.squeeze(i, [1]) for i in inputs]

            cell_state_size = concat_embedding.get_shape()[2].value

            # LSTM cell is replicated across stacks and timesteps.
            lstm_cell = rnn_cell.BasicLSTMCell(cell_state_size)
            cell = rnn_cell.MultiRNNCell([lstm_cell] * config.num_layers)

            # Both LSTMs have their own initial state.
            initial_state_fw = tf.get_variable("initial_state_fw", [1, cell.state_size])
            initial_state_bw = tf.get_variable("initial_state_bw", [1, cell.state_size])

            # Construct LSTM.
            outputs = rnn.bidirectional_rnn(cell, cell, inputs,
                                            initial_state_fw=tf.tile(initial_state_fw, [batch_size, 1]),
                                            initial_state_bw=tf.tile(initial_state_bw, [batch_size, 1]),
                                            sequence_length=self.num_tokens)

            # Rejoin LSTM outputs.
            outputs = tf.concat(1, outputs)
            outputs = tf.reshape(outputs, [batch_size, max_tokens, -1])

        with tf.name_scope("softmax"):
            # From LSTM outputs to softmax.
            penultimate = tf.tanh(self.linear_layer("penultimate", outputs, config.penultimate_hidden_size))
            softmax = self.linear_layer("softmax", penultimate, supertags_size)

        with tf.name_scope("prediction"):
            # Predictions are the indexes with the highest value from the softmax layer.
            self.prediction = tf.argmax(softmax, 2)

        with tf.name_scope("loss"):
            # Cross-entropy loss.
            pseudo_batch_size = batch_size * max_tokens

            self.loss = seq2seq.sequence_loss([tf.reshape(softmax, [pseudo_batch_size, -1])],
                                              [tf.reshape(self.y, [pseudo_batch_size])],
                                              [tf.ones([pseudo_batch_size])],
                                              supertags_size,
                                              average_across_timesteps=False,
                                              average_across_batch=False)

            # Only average across valid tokens rather than padding.
            self.loss = self.loss / tf.cast(tf.reduce_sum(self.num_tokens), tf.float32)

            self.params = tf.trainable_variables()
            if self.config.regularize:
                # Add L2 regularization for all trainable parameters.
                self.regularization = 1e-6 * sum(tf.nn.l2_loss(p) for p in self.params)
            else:
                self.regularization = 0.0

            self.cost = self.loss + self.regularization

        # Construct training operation.
        self.optimizer = tf.train.AdamOptimizer()

    # xs contains (batch, timestep, x)
    # Performs y = xw + b.
    # Returns the result containing (batch, timestep, x * w + b)
    def linear_layer(self, name, xs, y_dim):
        xs_dims = [d.value for d in xs.get_shape()]
        w = tf.get_variable("{}_w".format(name), [xs_dims[2], y_dim])
        b = tf.get_variable("{}_b".format(name), [y_dim])
        flattened_xs = tf.reshape(xs, [-1, xs_dims[2]])
        ys = tf.nn.xw_plus_b(flattened_xs, w, b)
        return tf.reshape(ys, [xs_dims[0], xs_dims[1], y_dim])

    def initialize(self, session):
        for name, space in self.config.embedding_spaces.items():
            if isinstance(space, features.PretrainedEmbeddingSpace):
                session.run(tf.assign(self.embeddings_w[name], space.embeddings))