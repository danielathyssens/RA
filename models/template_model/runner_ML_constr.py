#
# basic python imports
import os
import shutil
import logging
import warnings
from typing import Optional, Dict, List
from omegaconf import DictConfig, OmegaConf
import random
import numpy as np
import hydra
import torch
from torch.optim import Adam as Optimizer
from torch.optim.lr_scheduler import MultiStepLR as Scheduler
import time

# Model specific imports (Environment + Model to be initialised etc...)
from models.template_model.template_model_.utils import load_template_model
from models.template_model.template_model_.model import TemplateModel
# for example:
# from models.MDAM.MDAM.utils import load_problem, load_model_search
# from models.MDAM.MDAM.nets.model_search import AttentionModel

# train and eval functionality called from "template_model/template_model.py"
from models.template_model.template_model_ML import eval_model, train_model
# for on the fly training data generation - add transformation function from CVRPInstance to template_model-type data
from models.template_model.template_model_ML import prep_data
# for example:
# from models.MDAM.mdam import eval_model, train_model, prep_data_MDAM

# Benchmark Specific imports
from metrics.metrics import Metrics
from data.cvrp_dataset import CVRPDataset
from data.tsp_dataset import TSPDataset
# NamedTuple imports and utils:
from formats import RPSolution, CVRPInstance, TSPInstance
from models.runner_utils import _adjust_time_limit, merge_sols, print_summary_stats, set_device, \
    set_passMark, eval_inference, get_time_limit, NORMED_BENCHMARKS


logger = logging.getLogger(__name__)

# announce problem classes that the model can work with
DATA_CLASS = {
    'TSP': TSPDataset,
    'CVRP': CVRPDataset
}


class Runner:
    """wraps setup, training, testing of respective model
        experiments according to cfg"""

    def __init__(self, cfg: DictConfig):

        # fix path aliases changed by hydra
        self.cfg = update_path(cfg)
        OmegaConf.set_struct(self.cfg, False)

        # Model acronym
        # option for construction models to run with local search on top
        self.acronym, self.acronym_ls = self.get_acronym(model_name="template_model")

        # Name to identify run
        self.run_name = "{}_{}".format(self.cfg.run_type, self.acronym, time.strftime("%Y%m%dT%H%M%S"))

        # debug level
        if (self.cfg.run_type == "debug") or self.cfg.debug_lvl > 0:
            self.debug = max(self.cfg.debug_lvl, 1)
        else:
            self.debug = 0
        if self.debug > 1:
            torch.autograd.set_detect_anomaly(True)

        # set device
        self.device = set_device(self.cfg)

        # init metric
        self.metric = None
        self.per_instance_time_limit_constr = None
        self.per_instance_time_limit_ls = None
        self.machine_info = None

        # set PassMark for eval
        if cfg.run_type in ["val", "test"]:

            # get Time Budget --> if Time Budget is per instance (size), get_time_limit returns None
            self.time_limit = get_time_limit(self.cfg)

            # Option for ML-based Construction Heuristics to add or-tools based local search (ls) on top
            # this is to be specified in the configuration "test_cfg.add_ls"
            if self.cfg.test_cfg.add_ls:
                self.passMark, self.CPU_passMark = set_passMark(self.cfg, self.device,
                                                                self.cfg.test_cfg.ls_policy_cfg.search_workers)
                # get normalized per instance Time Limit
                if self.time_limit is not None:
                    # get a time limit for construction (mostly done on GPU) and one for the optional LS (done on CPU)
                    # in case the adj. budget for LS is smaller than for constr., only constr. will be performed
                    self.per_instance_time_limit_constr = _adjust_time_limit(self.time_limit, self.passMark,
                                                                             self.device)
                    self.per_instance_time_limit_ls = _adjust_time_limit(self.time_limit, self.CPU_passMark,
                                                                         torch.device("cpu"))
                    logger.info(f"Eval PassMark for {self.acronym}: {self.passMark}. "
                                f"Adjusted Time-Limit/Instance for Construction: {self.per_instance_time_limit_constr}."
                                f" Adjusted Time-Limit per Instance for Search : {self.per_instance_time_limit_ls}.")
                # time-budget determined implicitly --> machine-info passed to where instances are loaded
                else:
                    self.per_instance_time_limit_constr = None
                    self.machine_info = (self.passMark, self.CPU_passMark, self.device, 1, True)
                    logger.info(f"Per Instance Time Limit is set for each instance separately after loading data.")

            # Pure ML- construction setting
            else:
                self.passMark, self.CPU_passMark = set_passMark(self.cfg, self.device)
                if self.time_limit is not None:
                    self.per_instance_time_limit_constr = _adjust_time_limit(self.time_limit, self.passMark,
                                                                             self.device)
                    logger.info(f"Eval PassMark for {self.acronym}: {self.passMark}. "
                                f"Adjusted Time Limit per Instance: {self.per_instance_time_limit_constr}.")
                else:
                    self.per_instance_time_limit_constr = None
                    logger.info(f"Per Instance Time Limit is set for each instance separately after loading data.")
                    self.machine_info = (self.passMark, self.CPU_passMark, self.device, 1, False)

        else:
            self.passMark, self.CPU_passMark = None, None

    def setup(self):
        """set up all entities."""
        self._dir_setup()
        self.seed_all(self.cfg.global_seed)
        self._build_model()
        self._build_problem()  # aka build dataset
        # can only initialize the metrics class if we have an existing dataset path, valid entries for Passmark, device
        if self.cfg.run_type in ["val", "test"]:
            if self.cfg.data_file_path is not None and self.passMark is not None \
                    and self.cfg.test_cfg.eval_type != "simple":
                assert self.device in [torch.device("cpu"), torch.device("cuda")], \
                    f"Device {self.device} unknown - set to torch.device() for metric Evaluation " \
                    f"or set test_cfg.eval_type to 'simple'"
                self.init_metrics(self.cfg)
            # initialize or-tools local search class for additional LS on top
            if self.cfg.test_cfg.add_ls:
                self._build_policy_ls()

    def _dir_setup(self):
        """directories for logging, checkpoints, ..."""
        self._cwd = os.getcwd()
        self.cfg.tb_log_path = os.path.join(self._cwd, self.cfg.tb_log_path)
        # val log dir
        self.cfg.log_path = os.path.join(self._cwd, self.cfg.log_path)
        os.makedirs(self.cfg.log_path, exist_ok=True)
        # ckpt dir
        self.cfg.checkpoint_save_path = os.path.join(self._cwd, self.cfg.checkpoint_save_path)
        os.makedirs(self.cfg.checkpoint_save_path, exist_ok=True)

    def init_metrics(self, cfg):
        self.metric = Metrics(BKS=self.ds.bks,
                              passMark=self.passMark,
                              TimeLimit_=self.time_limit,
                              passMark_cpu=self.CPU_passMark,
                              base_sol_results=self.ds.BaseSol if self.ds.BaseSol else None,
                              scale_costs=10000 if os.path.basename(
                                  cfg.data_file_path) in NORMED_BENCHMARKS else None,
                              cpu=False if self.device != torch.device("cpu") else True,
                              is_cpu_search=cfg.test_cfg.add_ls,
                              single_thread=self.cfg.test_cfg.ls_policy_cfg.search_workers,
                              verbose=self.debug >= 1)
        # initialise metric in problem dataset class
        self.ds.metric = self.metric
        self.ds.adjusted_time_limit = self.per_instance_time_limit_constr if not cfg.test_cfg.add_ls \
            else self.per_instance_time_limit_ls

    def run(self):
        """Run experiment according to specified run_type."""
        if self.cfg.run_type in ['train', 'debug']:
            self.setup()
            self.train()
        elif self.cfg.run_type == 'resume':
            self.resume()
        elif self.cfg.run_type in ['val', 'test']:
            self.test()
        else:
            raise ValueError(f"unknown run_type: '{self.cfg.run_type}'. "
                             f"Must be one of ['train', 'resume', 'val', 'test', 'debug']")

    def _build_problem(self):
        """Load dataset and create environment (problem state and data)."""
        cfg = self.cfg.copy()

        if cfg.run_type in ["val", "test"]:
            self.ds = self.get_test_set(cfg)
        elif cfg.run_type in ["train", "resume"]:
            self.ds, self.val_data = self.get_train_val_set(cfg)
        else:
            raise NotImplementedError(f"Unknown run_type: '{self.cfg.run_type}' for model {self.acronym}"
                                      f"Must be ['val', 'test', 'train', 'resume']")

    def _build_model(self):
        """Infer and set the model/model arguments provided to the learning algorithm."""
        cfg = self.cfg.copy()
        if cfg.run_type in ["train", "debug", "resume"]:

            self.model = TemplateModel(
                ...
            ).to(self.device)

            # if cfg.cuda and torch.cuda.device_count() > 1:
            #     self.model = torch.nn.DataParallel(self.model)

            if cfg.run_type == "resume":
                # need to update state_dct from resuming chkpt and update nr of epochs before calling self.train()
                pass

        else:
            # loads model with arguments from state_dct in checkpoint and sets model to eval
            logger.info(f"Loading model for {self.acronym} on {self.device}...")
            # either internal model load functionality
            self.model, _ = load_template_model(self.cfg.test_cfg.checkpoint_load_path)
            # or load directly from ckpt
            self.model = TemplateModel(
                ...
            )
            model_filename = cfg.test_cfg.checkpoint_load_path
            # Overwrite model parameters by parameters to load
            load_data = torch.load(model_filename, map_location=lambda storage, loc: storage)  # Load on CPU
            self.model.load_state_dict({**self.model.state_dict(), **load_data.get('model', {})})
            self.model.eval()  # Put in eval mode
            self.model.to(self.device)

    def _build_policy_ls(self):
        """Load and prepare data and initialize GORT routing models."""
        from models.or_tools.or_tools import ParallelSolver
        policy_cfg = self.cfg.test_cfg.ls_policy_cfg.copy()
        self.policy_ls = ParallelSolver(
            problem=self.cfg.problem,
            solver_args=policy_cfg,
            time_limit=self.per_instance_time_limit_ls,
            num_workers=self.cfg.test_cfg.ls_policy_cfg.batch_size,  # instance processed in parallel
            search_workers=policy_cfg.search_workers
        )

    def save_results(self, result: Dict, run_id: int = 0):
        pth = os.path.join(self.cfg.log_path, "run_" + str(run_id) + "_results.pkl")
        torch.save(result, pth)

    def train(self, **kwargs):
        """Train the specified model."""

        cfg = self.cfg.copy()

        logger.info(f"start training on {self.device}...")
        results = train_model(
            ...
        )

        logger.info(f"training finished.")
        logger.info(f"Last results: {results[-1]}")

    def resume(self):
        """Resume training"""
        cfg = self.cfg.copy()
        if cfg.test_cfg.checkpoint_load_path is not None:
            epoch_resume = int(os.path.splitext(os.path.split(cfg.test_cfg.checkpoint_load_path)[-1])[0].split("-")[1])
            print("Resuming after {}".format(epoch_resume))
            epoch_start = epoch_resume + 1

            logger.info(f"resuming training...")
            _ = train_model(
                model=self.model,
                val_data_rp=self.ds_val.data,
                problem=cfg.problem,
                device=self.device,
                resume=True,
                resume_pth=cfg.test_cfg.checkpoint_load_path,
                epoch_start=epoch_start,
                opts=cfg.train_opts_cfg,
            )
            logger.info(f"training finished.")

        else:
            warnings.warn("No path specified to load data for resuming training. Default to normal training?")

    def test(self):
        """Test (evaluate) the trained model on specified dataset."""
        # make sure the problem is solvable
        assert self.cfg.problem.upper() in ["CVRP"], "Only CVRP implemented currently"
        # initialise all classes (Dataset, Model, Env, Metrics)
        self.setup()
        # default to a single run if number of runs not specified
        number_of_runs = self.cfg.number_runs if self.cfg.number_runs is not None else 1
        results_all, stats_all = [], []
        if self.cfg.test_cfg.add_ls and 1 < self.cfg.test_cfg.ls_policy_cfg.batch_size < len(self.ds.data):
            logger.info(f"Batching local search runs: running {self.cfg.test_cfg.ls_policy_cfg.batch_size} instances "
                        f"in parallel.")
        # start of inference loop
        for run in range(1, number_of_runs + 1):
            logger.info(f"running inference {run}/{number_of_runs}...")
            solutions_ = self.run_inference()
            logger.info(f"Starting Evaluation for run {run}/{number_of_runs} "
                        f"with time limit {self.time_limit} for {self.acronym}")
            results, summary_per_instance, stats = self.eval_inference(run, number_of_runs, solutions_)
            results_all.append(results)
            stats_all.append(stats)
        # if just one run - single run is saved in eval_inference and not here
        if number_of_runs > 1:
            print_summary_stats(stats_all, number_of_runs)
            # save overall list of results
            if self.cfg.test_cfg.save_solutions:
                logger.info(f"Storing Overall Results for {number_of_runs} runs in {os.path.join(self.cfg.log_path)}")
                self.save_results(
                    result={
                        "solutions": results_all,
                        "summary": stats_all,
                    })

    def run_inference(self) -> List[RPSolution]:
        # run test inference
        if self.cfg.test_cfg.add_ls:
            logger.info(
                f"Run-time dependent parameters: {self.device} Device "
                f"(threads: {self.cfg.test_cfg.ls_policy_cfg.batch_size}),"
                f" Adjusted Time Budget for construction: {self.per_instance_time_limit_constr} / instance."
                f" Adjusted Time Budget for LS: {self.per_instance_time_limit_ls} / instance.")
            construct_name = self.acronym.replace("_" + self.acronym_ls, "")
            logger.info(f"running test inference for {construct_name} with additional LS: {self.acronym_ls}...")
            # needs to return RPSolution (solution, cost, time, instance)
            _, solutions_construct = eval_model(self.model,
                                                self.ds.data,
                                                self.cfg.problem,
                                                batch_size=self.cfg.test_cfg.eval_batch_size,
                                                device=self.device,
                                                opts=self.cfg.eval_opts_cfg)
            costs_constr = [sol_.cost for sol_ in solutions_construct]
            time_constr = [sol_.run_time for sol_ in solutions_construct]
            if None not in costs_constr:
                logger.info(
                    f"Constructed solutions with average cost {np.mean(costs_constr)} in {np.mean(time_constr)}/inst")
            else:
                logger.info(f"{self.acronym} constructed inf. sols. Defaulting to GORT default construction (SAVINGS).")
            # check if not surpassed construction time budget and still have time for search in Time Budget
            # self.per_instance_time_limit_ls
            time_for_ls = self.per_instance_time_limit_ls if self.per_instance_time_limit_ls is not None \
                else np.mean([d.time_limit for d in self.ds.data])
            if np.mean(time_constr) < time_for_ls:
                logger.info(f"\n finished construction... starting LS")
                sols_search = self.policy_ls.solve(self.ds.data,
                                                   init_solution=solutions_construct,
                                                   distribution=self.cfg.coords_dist,
                                                   time_construct=float(np.mean(time_constr)))

                sols_ = merge_sols(sols_search, solutions_construct)
            else:
                sols_ = solutions_construct
                logger.info(f"Model {construct_name} used up runtime (on avg {np.mean(time_constr)}) for constructing "
                            f"(time limit {self.time_limit}). Using constructed solution for Evaluation.")
                self.acronym = construct_name
        else:
            # run test inference as construction method only
            logger.info(f"Run-time dependent parameters: {self.device} Device, "
                        f"Adjusted Time Budget for construction: {self.per_instance_time_limit_constr} / instance.")
            logger.info(f"running test inference for {self.acronym} as construction method...")
            _, sols_ = eval_model(self.model,
                                  self.ds.data,
                                  self.cfg.problem,
                                  batch_size=self.cfg.test_cfg.eval_batch_size,
                                  device=self.device,
                                  opts=self.cfg.eval_opts_cfg)

        return sols_

    def eval_inference(self, curr_run: int, number_of_runs: int, RP_solutions: List[RPSolution]):
        return eval_inference(
            curr_run,
            number_of_runs,
            RP_solutions,
            self.ds,
            self.cfg.log_path,
            self.acronym,
            self.cfg.test_cfg,
            self.debug
        )

    def get_test_set(self, cfg):
        if cfg.problem.upper() in DATA_CLASS.keys():
            dataset_class = DATA_CLASS[cfg.problem.upper()]
        else:
            raise NotImplementedError(f"Unknown problem class: '{self.cfg.problem.upper()}' for model {self.acronym}"
                                      f"Must be ['CVRP']")

        load_bks, load_base_sol = False, False
        if cfg.test_cfg.eval_type != "simple":
            load_bks = True
            if cfg.test_cfg.eval_type == "wrap" or "wrap" in cfg.test_cfg.eval_type:
                load_base_sol = True
            else:
                load_base_sol = False

        ds = dataset_class(
            store_path=cfg.test_cfg.data_file_path if 'data_file_path' in list(cfg.test_cfg.keys()) else None,
            distribution=cfg.coords_dist,
            graph_size=cfg.graph_size,
            dataset_size=cfg.test_cfg.dataset_size,
            seed=cfg.global_seed,
            TimeLimit=self.time_limit,
            machine_info=self.machine_info,
            load_base_sol=load_base_sol,
            load_bks=load_bks,
            verbose=self.debug >= 1,
            sampling_args=cfg.env_kwargs.sampling_args,
            generator_args=cfg.env_kwargs.generator_args
        )
        return ds

    def get_train_val_set(self, cfg):
        if cfg.problem.upper() in DATA_CLASS.keys():
            dataset_class = DATA_CLASS[cfg.problem.upper()]
        else:
            raise NotImplementedError(f"Unknown problem class: '{self.cfg.problem.upper()}' for model {self.acronym}"
                                      f"Must be ['CVRP']")
        ds = dataset_class(
            is_train=True,
            distribution=cfg.coords_dist,
            graph_size=cfg.graph_size,
            seed=cfg.global_seed,
            verbose=self.debug >= 1,
            device=self.device,
            transform_func=prep_data,
            sampling_args=cfg.env_kwargs.sampling_args,
            generator_args=cfg.env_kwargs.generator_args
        )

        ds_val = dataset_class(
            is_train=True,
            store_path=cfg.val_dataset if 'val_dataset' in list(cfg.keys()) else None,
            # default is None --> so generate ds_val
            num_samples=cfg.val_size,
            distribution=cfg.coords_dist,
            graph_size=cfg.graph_size,
            device=self.device,
            transform_func=prep_data,
            seed=cfg.global_seed,
            verbose=self.debug >= 1,
            sampling_args=cfg.env_kwargs.sampling_args,
            generator_args=cfg.env_kwargs.generator_args
        )
        val_data = ds_val.sample(cfg.val_size)
        torch.save(val_data, "val_dataset_for_train_run.pt")
        return ds, val_data

    def get_acronym(self, model_name):
        acronym, acronym_ls = model_name, None
        if self.cfg.run_type in ["val", "test"]:
            if self.cfg.test_cfg.add_ls:
                ls_policy = str(self.cfg.test_cfg.ls_policy_cfg.local_search_strategy).upper()
                acronym_ls = ''.join([word[0] for word in ls_policy.split("_")])
                # plain model acronym:
                acronym = model_name + acronym_ls
                # or add variants - for example:
                # if self.cfg.test_cfg.beam == 1:
                #     acronym = model_name + '_greedy_' + acronym_ls
                # if self.cfg.test_cfg.beam == 30:
                #     acronym = model_name + '_beam30_' + acronym_ls
            else:
                acronym = model_name
                # if self.cfg.test_cfg.beam == 1:
                #     acronym = model_name + '_greedy'
                # if self.cfg.test_cfg.beam == 30:
                #     acronym = model_name + '_beam30'
        return acronym, acronym_ls

    @staticmethod
    def seed_all(seed: int):
        """Set seed for all pseudo random generators."""
        # will set some redundant seeds, but better safe than sorry
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)


def update_path(cfg: DictConfig):
    """Correct the path to data files and checkpoints, since CWD is changed by hydra."""
    cwd = hydra.utils.get_original_cwd()

    if 'data_file_path' in list(cfg.keys()):
        cfg.data_file_path = os.path.normpath(
            os.path.join(cwd, cfg.data_file_path)
        )
    # if cfg.val_env_cfg.data_file_path is not None:
    #     cfg.val_env_cfg.data_file_path = os.path.normpath(
    #         os.path.join(cwd, cfg.val_env_cfg.data_file_path)
    #     )
    # if cfg.tester_cfg.test_env_cfg.data_file_path is not None:
    #     cfg.tester_cfg.test_env_cfg.data_file_path = os.path.normpath(
    #         os.path.join(cwd, cfg.tester_cfg.test_env_cfg.data_file_path)
    #     )

    if cfg.test_cfg.saved_res_dir is not None:
        cfg.test_cfg.saved_res_dir = os.path.normpath(
            os.path.join(cwd, cfg.test_cfg.saved_res_dir)
        )

    if cfg.test_cfg.checkpoint_load_path is not None:
        cfg.test_cfg.checkpoint_load_path = os.path.normpath(
            os.path.join(cwd, cfg.test_cfg.checkpoint_load_path)
        )
    return cfg


def remove_dir_tree(root: str, pth: Optional[str] = None):
    """Remove the full directory tree of the root directory if it exists."""
    if not os.path.isdir(root) and pth is not None:
        # select root directory from path by dir name
        i = pth.index(root)
        root = pth[:i + len(root)]
    if os.path.isdir(root):
        shutil.rmtree(root)
