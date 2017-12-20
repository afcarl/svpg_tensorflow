# -*- coding: utf-8 -*-
import tensorflow as tf
import numpy as np
import random
import time
import sys

from game_state import GameState
from game_state import ACTION_SIZE
from game_state import STATE_SIZE
from game_ac_network import GameACFFNetwork, GameACLSTMNetwork

from constants import GAMMA
from constants import LOCAL_T_MAX
from constants import ENTROPY_BETA
from constants import USE_LSTM

LOG_INTERVAL = 100
PERFORMANCE_LOG_INTERVAL = 1000

class A2CTrainingThread(object):
  def __init__(self,
               initial_learning_rate,
               learning_rate_input,
               grad_applier,
               max_global_time_step):

    self.learning_rate_input = learning_rate_input
    self.max_global_time_step = max_global_time_step
    self.game_state = GameState()
    state=self.game_state.reset();
    self.game_state.reset_gs(state);

    if USE_LSTM:
      self.local_network = GameACLSTMNetwork(ACTION_SIZE)
    else:
      self.local_network = GameACFFNetwork(ACTION_SIZE)

    self.local_network.prepare_loss(ENTROPY_BETA)

    var_refs = [v._ref() for v in self.local_network.get_vars()]
    self.gradients = tf.gradients(
      self.local_network.total_loss, var_refs,
      gate_gradients=False,
      aggregation_method=None,
      colocate_gradients_with_ops=False)

    self.apply_gradients = grad_applier.apply_gradients(
      self.local_network.get_vars(),
      self.gradients )
    
    self.local_t = 0

    self.initial_learning_rate = initial_learning_rate

    self.episode_reward = 0

    # variable controling log output
    self.prev_local_t = 0

  def _anneal_learning_rate(self, global_time_step):
    learning_rate = self.initial_learning_rate * (self.max_global_time_step - global_time_step) / self.max_global_time_step
    if learning_rate < 0.0:
      learning_rate = 0.0
    return learning_rate

  def choose_action(self, pi_values):
    return np.random.choice(range(len(pi_values)), p=pi_values)

  def _record_score(self, sess, summary_writer, summary_op, score_input, score, global_t):
    summary_str = sess.run(summary_op, feed_dict={
      score_input: score
    })
    summary_writer.add_summary(summary_str, global_t)
    summary_writer.flush()
    
  def set_start_time(self, start_time):
    self.start_time = start_time

  def process(self, sess, global_t, summary_writer, summary_op, score_input,score_ph="",score_ops=""):
    states = []
    actions = []
    rewards = []
    values = []

    terminal_end = False

    start_local_t = self.local_t

    if USE_LSTM:
      pstart_lstm_state = self.local_network.plstm_state_out
      vstart_lstm_state = self.local_network.vlstm_state_out

    # t_max times loop
    for i in range(LOCAL_T_MAX):
      action, value_ = self.local_network.run_policy_and_value(sess, self.game_state.s_t)
      states.append(self.game_state.s_t)
      actions.append(action)
      values.append(value_)

      # process game
      self.game_state.process(action)

      # receive game result
      reward = self.game_state.reward
      terminal = self.game_state.terminal

      self.episode_reward += reward

      # clip reward
      rewards.append( np.clip(reward, -1, 1) )

      self.local_t += 1

      # s_t1 -> s_t
      self.game_state.update()
      if terminal:
        terminal_end = True
        #score=self.episode_reward/self.game_state.r_sc;
        print("episode: "+str(global_t+1)+", score={}".format(self.episode_reward/self.game_state.r_sc))
        if summary_writer:
          self._record_score(sess, summary_writer, summary_op, score_input,
            self.episode_reward/self.game_state.r_sc, global_t)
        else:
          sess.run(score_ops,{score_ph:self.episode_reward/self.game_state.r_sc});
          
        self.episode_reward = 0
        state=self.game_state.reset()
        self.game_state.reset_gs(state);
        if USE_LSTM:
          self.local_network.reset_state()
        break

    R = 0.0
    if not terminal_end:
      R = self.local_network.run_value(sess, self.game_state.s_t)

    actions.reverse()
    states.reverse()
    rewards.reverse()
    values.reverse()

    batch_si = []
    batch_a = []
    batch_td = []
    batch_R = []

    # compute and accmulate gradients
    for(ai, ri, si, Vi) in zip(actions, rewards, states, values):
      R = ri + GAMMA * R
      td = R - Vi

      batch_si.append(si)
      batch_R.append(R)
      batch_td.append(td);

    cur_learning_rate = self._anneal_learning_rate(global_t)

    if USE_LSTM:
      batch_si.reverse()
      batch_td.reverse()
      batch_R.reverse()

      sess.run( self.apply_gradients,
                feed_dict = {
                  self.local_network.s: batch_si,
                  self.local_network.td: batch_td,
                  self.local_network.r: batch_R,
                  self.local_network.pinitial_lstm_state: pstart_lstm_state,
                  self.local_network.pstep_size : [len(batch_a)],
                  self.local_network.vinitial_lstm_state: vstart_lstm_state,
                  self.local_network.vstep_size : [len(batch_a)],
                  self.learning_rate_input: cur_learning_rate } )
    else:
      sess.run( self.apply_gradients,
                feed_dict = {
                  self.local_network.s: batch_si,
                  self.local_network.r: batch_R,
                  self.local_network.td: batch_td,
                  self.learning_rate_input: cur_learning_rate} )
      
    if ((global_t+1) - self.prev_local_t >= PERFORMANCE_LOG_INTERVAL):
      self.prev_local_t += PERFORMANCE_LOG_INTERVAL
      elapsed_time = time.time() - self.start_time
      steps_per_sec = global_t / elapsed_time
      print("### Performance : {} EPISODES in {:.0f} sec. {:.0f} EPISODES/sec. {:.2f}M EPISODES/hour".format(
        global_t,  elapsed_time, steps_per_sec, steps_per_sec * 3600 / 1000000.))
