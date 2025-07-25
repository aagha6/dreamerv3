import embodied
import jax
import jax.numpy as jnp
import ruamel.yaml as yaml
import escnn_jax.nn as nn
import escnn_jax.gspaces as gspaces

tree_map = jax.tree_util.tree_map
sg = lambda x: tree_map(jax.lax.stop_gradient, x)

import logging

logger = logging.getLogger()


class CheckTypesFilter(logging.Filter):
    def filter(self, record):
        return "check_types" not in record.getMessage()


logger.addFilter(CheckTypesFilter())

from . import behaviors
from . import jaxagent
from . import jaxutils
from . import nets
from . import ninjax as nj


@jaxagent.Wrapper
class Agent(nj.Module):

    configs = yaml.YAML(typ="safe").load(
        (embodied.Path(__file__).parent / "configs.yaml").read()
    )

    def __init__(self, obs_space, act_space, step, config, key):
        self.config = config
        self.obs_space = obs_space
        self.act_space = act_space["action"]
        self.step = step
        grp = None
        cup_catch = False
        if config.rssm.equiv:
            assert config.task in [
                "dmc_cartpole_swingup",
                "dmc_acrobot_swingup",
                "dmc_reacher_easy",
                "dmc_reacher_hard",
                "dmc_cup_catch",
                "dmc_pendulum_swingup",
            ], "Only DMC Cartpole Swingup task supports equivariance"
            if config.task in [
                "dmc_cartpole_swingup",
                "dmc_acrobot_swingup",
                "dmc_cup_catch",
                "dmc_pendulum_swingup",
            ]:
                grp = jaxutils.GroupHelper(gspace=gspaces.flip2dOnR2)
                if config.task == "dmc_cup_catch":
                    cup_catch = True
            elif "reacher" in config.task:
                grp = jaxutils.GroupHelper(gspace=gspaces.flipRot2dOnR2, n_rotations=2)
        wm_key, beh_key = jax.random.split(key, 2)
        if self.config.decoder.mlp_keys == "embed" and self.config.aug.swav:
            raise ValueError("decoding embedding and swav")
        self.wm = WorldModel(
            obs_space,
            act_space,
            config,
            grp=grp,
            name="wm",
            cup_catch=cup_catch,
            key=wm_key,
        )
        self.task_behavior = getattr(behaviors, config.task_behavior)(
            self.wm,
            self.act_space,
            self.config,
            key=beh_key,
            grp=grp,
            cup_catch=cup_catch,
            name="task_behavior",
        )
        if config.expl_behavior == "None":
            self.expl_behavior = self.task_behavior
        else:
            self.expl_behavior = getattr(behaviors, config.expl_behavior)(
                self.wm, self.act_space, self.config, name="expl_behavior"
            )

    def policy_initial(self, batch_size):
        return (
            self.wm.initial(batch_size),
            self.task_behavior.initial(batch_size),
            self.expl_behavior.initial(batch_size),
        )

    def train_initial(self, batch_size):
        return self.wm.initial(batch_size)

    def policy(self, obs, state, mode="train"):
        self.config.jax.jit and print("Tracing policy function.")
        obs = self.preprocess(obs, swav=False)
        (prev_latent, prev_action), task_state, expl_state = state
        embed = self.wm.encoder(obs)
        latent, _ = self.wm.rssm.obs_step(
            prev_latent, prev_action, embed, obs["is_first"]
        )
        self.expl_behavior.policy(latent, expl_state)
        task_outs, task_state = self.task_behavior.policy(latent, task_state)
        expl_outs, expl_state = self.expl_behavior.policy(latent, expl_state)
        if mode == "eval":
            outs = task_outs
            outs["action"] = outs["action"].sample(seed=nj.rng())
            outs["log_entropy"] = jnp.zeros(outs["action"].shape[:1])
        elif mode == "explore":
            outs = expl_outs
            outs["log_entropy"] = outs["action"].entropy()
            outs["action"] = outs["action"].sample(seed=nj.rng())
        elif mode == "train":
            outs = task_outs
            outs["log_entropy"] = outs["action"].entropy()
            outs["action"] = outs["action"].sample(seed=nj.rng())
        state = ((latent, outs["action"]), task_state, expl_state)
        return outs, state

    def train(self, data, state):
        self.config.jax.jit and print("Tracing train function.")
        metrics = {}
        data = self.preprocess(data, swav=self.config.aug.swav)
        if self.config.aug.swav:
            prev_latent, prev_action = state
            prev_latent = {
                k: jnp.concat([v, v], axis=0) for k, v in prev_latent.items()
            }
            prev_action = jnp.concat([prev_action, prev_action], axis=0)
            state = (prev_latent, prev_action)
        state, wm_outs, mets = self.wm.train(data, state)
        metrics.update(mets)
        context = {**data, **wm_outs["post"]}
        start = tree_map(lambda x: x.reshape([-1] + list(x.shape[2:])), context)
        _, mets = self.task_behavior.train(self.wm.imagine, start, context)
        metrics.update(mets)
        if self.config.expl_behavior != "None":
            _, mets = self.expl_behavior.train(self.wm.imagine, start, context)
            metrics.update({"expl_" + key: value for key, value in mets.items()})
        outs = {}
        if self.config.aug.swav:
            # TODO: we pass on one of the states as a prev state
            # to the next step, ultimately should promote invariance
            # to translation in the obs but not sure about this solution.
            state = tree_map(lambda x: jnp.split(x, 2, 0)[0], state)
        return outs, state, metrics

    def report(self, data):
        self.config.jax.jit and print("Tracing report function.")
        data = self.preprocess(data)
        report = {}
        report.update(self.wm.report(data))
        mets = self.task_behavior.report(data)
        report.update({f"task_{k}": v for k, v in mets.items()})
        if self.expl_behavior is not self.task_behavior:
            mets = self.expl_behavior.report(data)
            report.update({f"expl_{k}": v for k, v in mets.items()})
        return report

    def preprocess(self, obs, swav=False):
        obs = obs.copy()
        for key, value in obs.items():
            if key.startswith("log_") or key in ("key",):
                continue
            if len(value.shape) > 3 and value.dtype == jnp.uint8:
                value = jaxutils.cast_to_compute(value) / 255.0
            else:
                value = value.astype(jnp.float32)
            obs[key] = value
        obs["cont"] = 1.0 - obs["is_terminal"].astype(jnp.float32)
        # data augmentation
        if swav:
            obs = {k: jnp.concat([v, v], axis=0) for k, v in obs.items()}
            obs["image"] = jaxutils.random_translate(
                obs["image"], self.config.aug.max_delta
            )
        return obs


class WorldModel(nj.Module):

    def __init__(self, obs_space, act_space, config, key, cup_catch, grp=None):
        self.obs_space = obs_space
        self.act_space = act_space["action"]
        self.config = config
        shapes = {k: tuple(v.shape) for k, v in obs_space.items()}
        shapes["embed"] = (512,)
        shapes = {k: v for k, v in shapes.items() if not k.startswith("log_")}
        (
            rssm_key,
            encoder_key,
            ema_encoder_key,
            decoder_key,
            reward_key,
            cont_key,
            obs_proj_key,
            slow_obs_proj_key,
        ) = jax.random.split(key, 8)
        self.encoder = nets.MultiEncoder(
            shapes, encoder_key, **config.encoder, grp=grp, name="enc"
        )

        embed_size = None
        if config.rssm.equiv:
            if self.config.encoder.cnn == "frame_averaging":
                embed_size = 512
            else:
                embed_size = int(
                    config.encoder.cnn_depth // (grp.scaler**0.5) * (2**4) * 6
                )
        num_prototypes = config.batch_size * config.batch_length
        self.rssm = nets.RSSM(
            rssm_key,
            self.act_space.shape[0],
            **config.rssm,
            grp=grp,
            embed_size=embed_size,
            name="rssm",
            cup_catch=cup_catch,
            num_prototypes=num_prototypes if config.aug.swav else None,
        )
        if config.aug.swav:
            if self.config.encoder.cnn in ["pretrained", "frame_averaging"]:
                self._ema_encoder = self.encoder
            else:
                self._ema_encoder = nets.MultiEncoder(
                    shapes, ema_encoder_key, **config.encoder, grp=grp, name="slow_enc"
                )
            if config.rssm.equiv:
                gspace = grp.grp_act
                field_type_proj_in = nn.FieldType(
                    gspace, embed_size * [gspace.regular_repr]
                )
                field_type_proj_out = nn.FieldType(
                    gspace, config.rssm.proto * [gspace.regular_repr]
                )

                obs_proj_net = nn.R2Conv(
                    in_type=field_type_proj_in,
                    out_type=field_type_proj_out,
                    kernel_size=1,
                    key=obs_proj_key,
                )
                slow_obs_proj_net = nn.R2Conv(
                    in_type=field_type_proj_in,
                    out_type=field_type_proj_out,
                    kernel_size=1,
                    key=slow_obs_proj_key,
                )
                self._obs_proj = nets.EquivLinear(
                    net=obs_proj_net,
                    in_type=field_type_proj_in,
                    out_type=field_type_proj_out,
                    norm="none",
                    act="none",
                    name="obs_proj",
                )
                self._ema_obs_proj = nets.EquivLinear(
                    net=slow_obs_proj_net,
                    in_type=field_type_proj_in,
                    out_type=field_type_proj_out,
                    norm="none",
                    act="none",
                    name="ema_proj",
                )
            else:
                self._obs_proj = nets.Linear(units=config.rssm.proto, name="obs_proj")
                self._ema_obs_proj = nets.Linear(
                    units=config.rssm.proto, name="ema_proj"
                )

            if self.config.encoder.cnn not in ["pretrained", "frame_averaging"]:
                self._encoder_updater = jaxutils.SlowUpdater(
                    self.encoder,
                    self._ema_encoder,
                    self.config.slow_critic_fraction,
                    self.config.slow_critic_update,
                )
            self._proj_updater = jaxutils.SlowUpdater(
                self._obs_proj,
                self._ema_obs_proj,
                self.config.slow_critic_fraction,
                self.config.slow_critic_update,
            )
        self.heads = {}
        if not config.aug.swav:
            self.heads["decoder"] = nets.MultiDecoder(
                shapes,
                decoder_key,
                deter=config.rssm["deter"],
                stoch=(
                    config.rssm["stoch"] * config.rssm["classes"]
                    if config.rssm["classes"]
                    else config.rssm["stoch"]
                ),
                **config.decoder,
                grp=grp,
                name="dec",
            )
        if config.reward_head["equiv"]:
            self.heads["reward"] = nets.EquivMLP(
                (),
                deter=config.rssm["deter"],
                stoch=(
                    config.rssm["stoch"] * config.rssm["classes"]
                    if config.rssm["classes"]
                    else config.rssm["stoch"]
                ),
                key=reward_key,
                **config.reward_head,
                grp=grp,
                name="rew",
            )
        else:
            self.heads["reward"] = nets.MLP((), **config.reward_head, name="rew")

        if config.cont_head["equiv"]:
            self.heads["cont"] = nets.EquivMLP(
                (),
                deter=config.rssm["deter"],
                stoch=(
                    config.rssm["stoch"] * config.rssm["classes"]
                    if config.rssm["classes"]
                    else config.rssm["stoch"]
                ),
                key=cont_key,
                **config.cont_head,
                grp=grp,
                name="cont",
            )
        else:
            self.heads["cont"] = nets.MLP((), **config.cont_head, name="cont")

        self.opt = jaxutils.Optimizer(name="model_opt", **config.model_opt)
        scales = self.config.loss_scales.copy()
        image, vector = scales.pop("image"), scales.pop("vector")
        if "decoder" in self.heads.keys():
            scales.update({k: image for k in self.heads["decoder"].cnn_shapes})
            scales.update({k: vector for k in self.heads["decoder"].mlp_shapes})
        self.scales = scales

    def initial(self, batch_size):
        prev_latent = self.rssm.initial(batch_size)
        prev_action = jnp.zeros((batch_size, *self.act_space.shape))
        return prev_latent, prev_action

    def train(self, data, state):
        modules = [self.rssm, *self.heads.values()]
        if self.config.encoder.cnn not in ["pretrained", "frame_averaging"]:
            modules += [self.encoder]
        if self.config.aug.swav:
            modules += [self._obs_proj]
        mets, (state, outs, metrics) = self.opt(
            modules, self.loss, data, state, has_aux=True
        )
        metrics.update(mets)
        if self.config.aug.swav:
            if self.config.encoder.cnn not in ["pretrained", "frame_averaging"]:
                self._encoder_updater()
            self._proj_updater()
        return state, outs, metrics

    def ema_proj(self, data):
        embed = self._ema_encoder(data)
        if self.rssm._equiv:
            proj = self._ema_obs_proj(embed.reshape([-1] + list(embed.shape[2:])))
            proj = proj.reshape(embed.shape[:2] + (-1,))
        else:
            proj = self._ema_obs_proj(embed)
        return proj

    def loss(self, data, state):
        embed = self.encoder(data)
        if self.config.decoder.mlp_keys == "embed":
            data["embed"] = embed
        prev_latent, prev_action = state
        prev_actions = jnp.concatenate(
            [prev_action[:, None], data["action"][:, :-1]], 1
        )
        post, prior = self.rssm.observe(
            embed, prev_actions, data["is_first"], prev_latent
        )
        dists = {}
        feats = {**post, "embed": embed}
        for name, head in self.heads.items():
            out = head(feats if name in self.config.grad_heads else sg(feats))
            out = out if isinstance(out, dict) else {name: out}
            dists.update(out)
        losses = {}
        if self.config.aug.swav:
            if self.rssm._equiv:
                obs_proj = self._obs_proj(embed.reshape([-1] + list(embed.shape[2:])))
                obs_proj = obs_proj.reshape(embed.shape[:2] + (-1,))
            else:
                obs_proj = self._obs_proj(embed)
            ema_proj = jax.lax.stop_gradient(self.ema_proj(data))
            losses = self.rssm.proto_loss(
                post=post, obs_proj=obs_proj, ema_proj=ema_proj
            )
        losses["dyn"] = self.rssm.dyn_loss(post, prior, **self.config.dyn_loss)
        losses["rep"] = self.rssm.rep_loss(post, prior, **self.config.rep_loss)
        for key, dist in dists.items():
            loss = -dist.log_prob(data[key].astype(jnp.float32))
            assert loss.shape == embed.shape[:2], (key, loss.shape)
            losses[key] = loss
        scaled = {k: v * self.scales[k] for k, v in losses.items()}
        model_loss = sum(scaled.values())
        out = {"embed": embed, "post": post, "prior": prior}
        out.update({f"{k}_loss": v for k, v in losses.items()})
        last_latent = {k: v[:, -1] for k, v in post.items()}
        last_action = data["action"][:, -1]
        state = last_latent, last_action
        metrics = self._metrics(data, dists, post, prior, losses, model_loss)
        return model_loss.mean(), (state, out, metrics)

    def imagine(self, policy, start, horizon):
        first_cont = (1.0 - start["is_terminal"]).astype(jnp.float32)
        keys = list(self.rssm.initial(1).keys())
        start = {k: v for k, v in start.items() if k in keys}
        start["action"] = policy(start)

        def step(prev, _):
            prev = prev.copy()
            state = self.rssm.img_step(prev, prev.pop("action"))
            return {**state, "action": policy(state)}

        traj = jaxutils.scan(step, jnp.arange(horizon), start, self.config.imag_unroll)
        traj = {k: jnp.concatenate([start[k][None], v], 0) for k, v in traj.items()}
        cont = self.heads["cont"](traj).mode()
        traj["cont"] = jnp.concatenate([first_cont[None], cont[1:]], 0)
        discount = 1 - 1 / self.config.horizon
        traj["weight"] = jnp.cumprod(discount * traj["cont"], 0) / discount
        return traj

    def report(self, data):
        state = self.initial(len(data["is_first"]))
        report = {}
        report.update(self.loss(data, state)[-1][-1])
        context, _ = self.rssm.observe(
            self.encoder(data)[:6, :5], data["action"][:6, :5], data["is_first"][:6, :5]
        )
        start = {k: v[:, -1] for k, v in context.items()}
        if "decoder" in self.heads:
            recon = self.heads["decoder"](context)
            openl = self.heads["decoder"](
                self.rssm.imagine(data["action"][:6, 5:], start)
            )
            for key in self.heads["decoder"].cnn_shapes.keys():
                truth = data[key][:6].astype(jnp.float32)
                model = jnp.concatenate(
                    [recon[key].mode()[:, :5], openl[key].mode()], 1
                )
                error = (model - truth + 1) / 2
                video = jnp.concatenate([truth, model, error], 2)
                report[f"openl_{key}"] = jaxutils.video_grid(video)
        return report

    def _metrics(self, data, dists, post, prior, losses, model_loss):
        entropy = lambda feat: self.rssm.get_dist(feat).entropy()
        metrics = {}
        metrics.update(jaxutils.tensorstats(entropy(prior), "prior_ent"))
        metrics.update(jaxutils.tensorstats(entropy(post), "post_ent"))
        metrics.update({f"{k}_loss_mean": v.mean() for k, v in losses.items()})
        metrics.update({f"{k}_loss_std": v.std() for k, v in losses.items()})
        metrics["model_loss_mean"] = model_loss.mean()
        metrics["model_loss_std"] = model_loss.std()
        metrics["reward_max_data"] = jnp.abs(data["reward"]).max()
        metrics["reward_max_pred"] = jnp.abs(dists["reward"].mean()).max()
        if "reward" in dists and not self.config.jax.debug_nans:
            stats = jaxutils.balance_stats(dists["reward"], data["reward"], 0.1)
            metrics.update({f"reward_{k}": v for k, v in stats.items()})
        if "cont" in dists and not self.config.jax.debug_nans:
            stats = jaxutils.balance_stats(dists["cont"], data["cont"], 0.5)
            metrics.update({f"cont_{k}": v for k, v in stats.items()})
        return metrics


class ImagActorCritic(nj.Module):

    def __init__(
        self, critics, scales, act_space, config, grp, actor_key, cup_catch=False
    ):
        critics = {k: v for k, v in critics.items() if scales[k]}
        for key, scale in scales.items():
            assert not scale or key in critics, key
        self.critics = {k: v for k, v in critics.items() if scales[k]}
        self.scales = scales
        self.act_space = act_space
        self.config = config
        disc = act_space.discrete
        self.grad = config.actor_grad_disc if disc else config.actor_grad_cont
        if config.rssm.equiv:
            self.cup_catch = cup_catch
            self.actor = nets.EquivMLP(
                name="actor",
                invariant=False,
                deter=config.rssm["deter"],
                grp=grp,
                key=actor_key,
                stoch=(
                    config.rssm["stoch"] * config.rssm["classes"]
                    if config.rssm["classes"]
                    else config.rssm["stoch"]
                ),
                shape=act_space.shape,
                **config.actor,
                cup_catch=self.cup_catch,
                dist=config.actor_dist_disc if disc else config.actor_dist_cont,
            )
        else:
            self.actor = nets.MLP(
                name="actor",
                dims="deter",
                shape=act_space.shape,
                **config.actor,
                dist=config.actor_dist_disc if disc else config.actor_dist_cont,
            )
        self.retnorms = {
            k: jaxutils.Moments(**config.retnorm, name=f"retnorm_{k}") for k in critics
        }
        self.opt = jaxutils.Optimizer(name="actor_opt", **config.actor_opt)

    def initial(self, batch_size):
        return {}

    def policy(self, state, carry):
        return {"action": self.actor(state)}, carry

    def train(self, imagine, start, context):
        def loss(start):
            policy = lambda s: self.actor(sg(s)).sample(seed=nj.rng())
            traj = imagine(policy, start, self.config.imag_horizon)
            loss, metrics = self.loss(traj)
            return loss, (traj, metrics)

        mets, (traj, metrics) = self.opt(self.actor, loss, start, has_aux=True)
        metrics.update(mets)
        for key, critic in self.critics.items():
            mets = critic.train(traj, self.actor)
            metrics.update({f"{key}_critic_{k}": v for k, v in mets.items()})
        return traj, metrics

    def loss(self, traj):
        metrics = {}
        advs = []
        total = sum(self.scales[k] for k in self.critics)
        for key, critic in self.critics.items():
            rew, ret, base = critic.score(traj, self.actor)
            offset, invscale = self.retnorms[key](ret)
            normed_ret = (ret - offset) / invscale
            normed_base = (base - offset) / invscale
            advs.append((normed_ret - normed_base) * self.scales[key] / total)
            metrics.update(jaxutils.tensorstats(rew, f"{key}_reward"))
            metrics.update(jaxutils.tensorstats(ret, f"{key}_return_raw"))
            metrics.update(jaxutils.tensorstats(normed_ret, f"{key}_return_normed"))
            metrics[f"{key}_return_rate"] = (jnp.abs(ret) >= 0.5).mean()
        adv = jnp.stack(advs).sum(0)
        policy = self.actor(sg(traj))
        logpi = policy.log_prob(sg(traj["action"]))[:-1]
        loss = {"backprop": -adv, "reinforce": -logpi * sg(adv)}[self.grad]
        ent = policy.entropy()[:-1]
        loss -= self.config.actent * ent
        loss *= sg(traj["weight"])[:-1]
        loss *= self.config.loss_scales.actor
        metrics.update(self._metrics(traj, policy, logpi, ent, adv))
        return loss.mean(), metrics

    def _metrics(self, traj, policy, logpi, ent, adv):
        metrics = {}
        ent = policy.entropy()[:-1]
        rand = (ent - policy.minent) / (policy.maxent - policy.minent)
        rand = rand.mean(range(2, len(rand.shape)))
        act = traj["action"]
        act = jnp.argmax(act, -1) if self.act_space.discrete else act
        metrics.update(jaxutils.tensorstats(act, "action"))
        metrics.update(jaxutils.tensorstats(rand, "policy_randomness"))
        metrics.update(jaxutils.tensorstats(ent, "policy_entropy"))
        metrics.update(jaxutils.tensorstats(logpi, "policy_logprob"))
        metrics.update(jaxutils.tensorstats(adv, "adv"))
        metrics["imag_weight_dist"] = jaxutils.subsample(traj["weight"])
        return metrics


class VFunction(nj.Module):

    def __init__(self, rewfn, config, grp, key):
        self.rewfn = rewfn
        self.config = config
        if config.rssm.equiv:
            keys = jax.random.split(key, 2)
            self.net = nets.InvMLP(
                (),
                deter=config.rssm["deter"],
                stoch=(
                    config.rssm["stoch"] * config.rssm["classes"]
                    if config.rssm["classes"]
                    else config.rssm["stoch"]
                ),
                **self.config.critic,
                grp=grp,
                key=keys[0],
                dims="deter",
                name="net",
            )
            self.slow = nets.InvMLP(
                (),
                deter=config.rssm["deter"],
                stoch=(
                    config.rssm["stoch"] * config.rssm["classes"]
                    if config.rssm["classes"]
                    else config.rssm["stoch"]
                ),
                **self.config.critic,
                grp=grp,
                key=keys[1],
                dims="deter",
                name="slow",
            )
        else:
            self.net = nets.MLP((), name="net", dims="deter", **self.config.critic)
            self.slow = nets.MLP((), name="slow", dims="deter", **self.config.critic)
        self.updater = jaxutils.SlowUpdater(
            self.net,
            self.slow,
            self.config.slow_critic_fraction,
            self.config.slow_critic_update,
        )
        self.opt = jaxutils.Optimizer(name="critic_opt", **self.config.critic_opt)

    def train(self, traj, actor):
        target = sg(self.score(traj)[1])
        mets, metrics = self.opt(self.net, self.loss, traj, target, has_aux=True)
        metrics.update(mets)
        self.updater()
        return metrics

    def loss(self, traj, target):
        metrics = {}
        traj = {k: v[:-1] for k, v in traj.items()}
        dist = self.net(traj)
        loss = -dist.log_prob(sg(target))
        if self.config.critic_slowreg == "logprob":
            reg = -dist.log_prob(sg(self.slow(traj).mean()))
        elif self.config.critic_slowreg == "xent":
            reg = -jnp.einsum(
                "...i,...i->...", sg(self.slow(traj).probs), jnp.log(dist.probs)
            )
        else:
            raise NotImplementedError(self.config.critic_slowreg)
        loss += self.config.loss_scales.slowreg * reg
        loss = (loss * sg(traj["weight"])).mean()
        loss *= self.config.loss_scales.critic
        metrics = jaxutils.tensorstats(dist.mean())
        return loss, metrics

    def score(self, traj, actor=None):
        rew = self.rewfn(traj)
        assert (
            len(rew) == len(traj["action"]) - 1
        ), "should provide rewards for all but last action"
        discount = 1 - 1 / self.config.horizon
        disc = traj["cont"][1:] * discount
        value = self.net(traj).mean()
        vals = [value[-1]]
        interm = rew + disc * value[1:] * (1 - self.config.return_lambda)
        for t in reversed(range(len(disc))):
            vals.append(interm[t] + disc[t] * self.config.return_lambda * vals[-1])
        ret = jnp.stack(list(reversed(vals))[:-1])
        return rew, ret, value[:-1]
