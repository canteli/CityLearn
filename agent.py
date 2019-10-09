import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
import collections
import numpy as np
import random
import copy

class DDPGActor(nn.Module):
    def __init__(self, obs_size, act_size):
        super(DDPGActor, self).__init__()

        self.net = nn.Sequential(
            nn.Linear(obs_size, 4),
            nn.ReLU(),
            nn.Linear(4, 4),
            nn.ReLU(),
            nn.Linear(4, act_size),
            nn.Tanh()
        )

    def forward(self, x):
        return self.net(x)

class DDPGCritic(nn.Module):
    def __init__(self, obs_size, act_size):
        super(DDPGCritic, self).__init__()

        self.obs_net = nn.Sequential(
            nn.Linear(obs_size, 8),
            nn.ReLU(),
        )

        self.out_net = nn.Sequential(
            nn.Linear(8 + act_size, 6),
            nn.ReLU(),
            nn.Linear(6, 1)
        )

    def forward(self, x, a):
        obs = self.obs_net(x)
        return self.out_net(torch.cat([obs, a], dim=1))
    
class TargetNet:
    """
    Wrapper around model which provides copy of it instead of trained weights
    """
    def __init__(self, model):
        self.model = model
        self.target_model = copy.deepcopy(model)

    def sync(self):
        self.target_model.load_state_dict(self.model.state_dict())

    def alpha_sync(self, alpha):
        """
        Blend params of target net with params from the model
        :param alpha:
        """
        assert isinstance(alpha, float)
        assert 0.0 < alpha <= 1.0
        state = self.model.state_dict()
        tgt_state = self.target_model.state_dict()
        for k, v in state.items():
            tgt_state[k] = tgt_state[k] * alpha + (1 - alpha) * v
        self.target_model.load_state_dict(tgt_state)
        
    
class Batch:
    def __init__(self):
        self.batch = []
        
    def append_sample(self, sample):
        self.batch.append(sample)
        
    def sample(self, sample_size):
        s, a, r, s_next = [],[],[],[]
        
        if sample_size > len(self.batch):
            sample_size = len(self.batch)
            
        rand_sample = random.sample(self.batch, sample_size)
        for values in rand_sample:
            s.append(values[0])
            a.append(values[1])
            r.append(values[2])
            s_next.append(values[3])
        return torch.tensor(s,dtype=torch.float32), torch.tensor(a,dtype=torch.float32), torch.tensor(r,dtype=torch.float32), torch.tensor(s_next,dtype=torch.float32)
    
    def __len__(self):
         return len(self.batch)
        
    
class RL_Agents:
    def __init__(self, observation_spaces = None, action_spaces = None):
        self.device = "cpu"
        self.epsilon = 1.2
        self.n_buildings = len(observation_spaces)
        self.batch = {}
        self.frame_idx = {}
        for i in range(len(observation_spaces)):
            self.batch[i] = Batch()
            
        LEARNING_RATE_ACTOR = 1e-4
        LEARNING_RATE_CRITIC = 1e-3
        self.MIN_REPLAY_MEMORY = 100
        self.BATCH_SIZE = 2400
        self.EPOCHS = 6
        self.GAMMA = 0.99
        self.EPSILON_FINAL = 0.01
        self.EPSILON_START = 1.2
        self.EPSILON_DECAY_LAST_FRAME = 5000
        self.hour_idx = 0
        
        i = 0
        self.act_net, self.crt_net, self.tgt_act_net, self.tgt_crt_net, self.act_opt, self.crt_opt = {}, {}, {}, {}, {}, {}
        for o, a in zip(observation_spaces, action_spaces):
            self.act_net[i] = DDPGActor(o.shape[0], a.shape[0]).to(self.device)
            self.crt_net[i] = DDPGCritic(o.shape[0], a.shape[0]).to(self.device)
            self.tgt_act_net[i] = TargetNet(self.act_net[i])
            self.tgt_crt_net[i] = TargetNet(self.crt_net[i])
            self.act_opt[i] = optim.Adam(self.act_net[i].parameters(), lr=LEARNING_RATE_ACTOR)
            self.crt_opt[i] = optim.Adam(self.crt_net[i].parameters(), lr=LEARNING_RATE_CRITIC)
            i += 1
        
    def select_action(self, states):
        i, actions = 0, []
        for state in states:
            a = 0.5*self.act_net[i](torch.tensor(state))
            a = a.cpu().detach().numpy() + self.epsilon * 0.5 * np.random.normal(size=a.shape)
            a = np.clip(a, -0.5, 0.5)
            actions.append(a)
            i += 1
        return actions
    
    def add_to_batch(self, states, actions, rewards, next_states):
        i = 0
        for s, a, r, s_next in zip(states, actions, rewards, next_states):
            self.batch[i].append_sample((s, a, r, s_next))
            i += 1
            
        batch, states_v, actions_v, rewards_v, dones_mask, states_next_v, q_v, last_act_v, q_last_v, q_ref_v, critic_loss_v, cur_actions_v, actor_loss_v = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}  
        
        self.epsilon = max(self.EPSILON_FINAL, self.EPSILON_START - self.hour_idx / self.EPSILON_DECAY_LAST_FRAME)
        self.hour_idx += 1
        for i in range(self.n_buildings):
            if len(self.batch[i]) > self.MIN_REPLAY_MEMORY:
                for k in range(self.EPOCHS):
                    states_v[i], actions_v[i], rewards_v[i], states_next_v[i] = self.batch[i].sample(self.BATCH_SIZE)

                    # TRAIN CRITIC
                    self.crt_opt[i].zero_grad()
                    #Obtaining Q' using critic net with parameters teta_Q'
                    q_v[i] = self.crt_net[i](states_v[i], actions_v[i])

                    #Obtaining estimated optimal actions a|teta_mu from target actor net and from s_i+1.
                    last_act_v[i] = self.tgt_act_net[i].target_model(states_next_v[i]) #<----- Actor to train Critic

                    #Obtaining Q'(s_i+1, a|teta_mu) from critic net Q'
                    q_last_v[i] = self.tgt_crt_net[i].target_model(states_next_v[i], last_act_v[i])
#                     q_last_v[i][dones_mask[i]] = 0.0

                    #Q_target used to train critic net Q'
                    q_ref_v[i] = rewards_v[i].unsqueeze(dim=-1) + q_last_v[i] * self.GAMMA
                    critic_loss_v[i] = F.mse_loss(q_v[i], q_ref_v[i].detach())
                    critic_loss_v[i].backward()
                    self.crt_opt[i].step()

                    # TRAIN ACTOR
                    self.act_opt[i].zero_grad()
                    #Obtaining estimated optimal current actions a|teta_mu from actor net and from s_i
                    cur_actions_v[i] = self.act_net[i](states_v[i])

                    #Actor loss = mean{ -Q_i'(s_i, a|teta_mu) }
                    actor_loss_v[i] = -self.crt_net[i](states_v[i], cur_actions_v[i]) #<----- Critic to train Actor
                    actor_loss_v[i] = actor_loss_v[i].mean()
                    #Find gradient of the loss and backpropagate to perform the updates of teta_mu
                    actor_loss_v[i].backward()
                    self.act_opt[i].step()

                    self.tgt_act_net[i].alpha_sync(alpha=1 - 0.1)
                    self.tgt_crt_net[i].alpha_sync(alpha=1 - 0.1)