#
import warnings
import os
import logging
from typing import Optional, Dict, Union, NamedTuple, List, Tuple
from abc import abstractmethod
from timeit import default_timer

import numpy as np
import matplotlib.pyplot as plt
from multiprocessing import Pool
from tqdm import tqdm
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# from lib.routing import RPInstance, RPSolution
from formats import CVRPInstance, TSPInstance, RPSolution
from visualization import Viewer

# from baselines.CVRP.formats import CVRP_DEFAULTS

logger = logging.getLogger(__name__)
STATUS = {
    0: 'ROUTING_NOT_SOLVED',
    1: 'ROUTING_SUCCESS',
    2: 'ROUTING_FAIL',
    3: 'ROUTING_FAIL_TIMEOUT',
    4: 'ROUTING_INVALID',
}
CVRP_DEFAULTS = {   # num vehicles and integer capacity per problem size
    20: [8, 30],
    50: [16, 40],
    100: [32, 50],
    200: [48, 50],
    500: [64, 50],
    1000: [128, 50],
}



class GORTInstance(NamedTuple):
    """Typed instance format for GORT solver."""
    depot_idx: int
    n: int
    k: int
    locations: List[List[int]]
    demands: List[int]
    capacity: Union[List[int], int]
    dist_mat: Optional[Union[List, Dict]] = None
    time_windows: Optional[List[Union[Tuple, List]]] = None
    service_times: Optional[List[int]] = None
    service_horizon: Optional[List[int]] = None


def is_feasible(solution, features, verbose=False, start=None, end=None):
    #if start is None:
    #    start = 0
    #if end is None:
    #    end = len(solution)
    # print('len(solution)', len(solution))
    # max vehicle check:
    # if len(solution) > features['vehicle_max']:
    #    warn(f"num_tours > max vehicles!")
    # print('solution', solution)
    # check capacity constraint:
    cap = features.capacity[0]
    # print('cap', cap)
    for t_idx, tour in enumerate(solution):
        # print('t_idx, tour', t_idx, tour)
        tour_demand = 0
        for i, node in enumerate(tour):
            tour_demand += features.demands[node]
            # print('tour_demand', tour_demand)
            if tour_demand > cap:
                raise RuntimeError(f"Capacity constraint violated in tour={t_idx} at {tour[i-1]}->{node}:"
                                   f"\n     demand {tour_demand} > cap {cap}!"
                                   f"\n     tour: {tour},"
                                   f"\n     tour_demands: {[features.demands[idx] for idx in tour]}")

    return True

def clockit_return(method):
    """Decorator to measure and report time elapsed"""

    def timed(*args, **kw):
        start = default_timer()
        result = method(*args, **kw)
        end = default_timer()
        time_elapsed = (end - start)
        return result, time_elapsed

    return timed


def get_sol(manager, routing, data):
    routes = []
    for vehicle_id in range(data.k):
        index = routing.Start(vehicle_id)  # tour start index
        route = []

        while not routing.IsEnd(index):
            # previous_index = index
            node = manager.IndexToNode(index)
            route.append(node)
            index = routing.NextVar(index).Value()
        routes.append(route)
    routes_final = [rout for rout in routes if len(rout) > 1]
    return routes_final


class SearchTrajectoryCallback:
    """Search monitor to call every time an (intermediate) solution is found"""

    def __init__(self, model):
        self.model = model
        self.buffer = []

    def __call__(self):
        self.buffer.append(self.model.CostVar().Min())


class SolutionCallback:

    def __init__(self, model, manager, data):
        self.model = model
        self.manager = manager
        self.data = data
        self.time_start = default_timer()
        self.buffer = []
        self.buffer_time = []
        self.prev_cost = float('inf')

    def __call__(self):
        time_now = default_timer()
        # RoutingSearchParameters.LocalSearchNeighborhoodOperatorsOrBuilder
        # print('curr cost', self.model.CostVar().Min())
        # print('curr time elapsed', time_now - self.time_start)
        self.buffer_time.append(time_now - self.time_start)
        self.buffer.append(get_sol(self.manager, self.model, self.data))
        # curr_run_time = None
        # sol = get_sol(self.manager, self.model, self.data)
        # print('curr sol', sol)
        # curr_cost = self.model.CostVar().Min()
        # print('self.prev_cost', self.prev_cost)
        # print('curr_cost', curr_cost)
        # print('self.buffer', self.buffer)
        # print('self.buffer_time', self.buffer_time)
        # if self.model.CostVar().Min() < self.prev_cost:
        #     print('NEW BEST SOL FOUND AT', time_now - self.time_start)
        #     self.prev_cost = self.model.CostVar().Min()
        # self.buffer.append((curr_run_time, sol))


# class RunTimeCallback:
#
#     def __init__(self, model):
#         self.model = model
#         self.time_start = default_timer()
#         self.buffer = []
#         self.prev_cost = float('inf')

#     def __call__(self):
#         time_now = default_timer()
#         curr_cost = self.model.CostVar().Min()
#         if curr_cost < self.prev_cost:
#             self.buffer.append(time_now - self.time_start)
#             self.prev_cost = curr_cost


class RoutingSolver:
    """General (abstract) Routing Problem Solver class"""

    def __init__(self):
        self.manager = None
        self.model = None
        self.search_trajectory_callback = None
        self.solution_callback = None
        self.solution = None
        self.data = None
        self.callbacks = {}
        self.cumul_vars = []
        self.dummy_indices = []  # data indices to dummy nodes, different from GORT indices!

    def _model_status(self):
        """Get the status of the model"""
        status_id = self.model.status()
        return STATUS.get(status_id, 'UNKNOWN')

    def _monitor_search(self):
        """Adding search monitor callback"""
        self.search_trajectory_callback = SearchTrajectoryCallback(self.model)
        self.model.AddAtSolutionCallback(self.search_trajectory_callback)

        self.solution_callback = SolutionCallback(self.model, self.manager, self.data)
        self.model.AddAtSolutionCallback(self.solution_callback)

        # self.runtime_log = RunTimeCallback(self.model)
        # self.model.AddAtSolutionCallback(self.runtime_log)

    @abstractmethod
    def create_model(self, data: GORTInstance, **kwargs):
        """Creates model and adds data, cost evaluators, dimensions and constraints"""
        raise NotImplementedError

    @abstractmethod
    def create_callbacks(self):
        """Creates all necessary data callbacks"""
        raise NotImplementedError

    def parse_assignment(self, assignment):
        """Get some information from the assignment object"""
        cum_dims = {dim: [] for dim in self.cumul_vars}
        routes, transit_costs = [], []

        for vehicle_id in range(self.data.k):

            index = self.model.Start(vehicle_id)  # tour start index
            route = []
            transit_cost = []
            cum_dim = {dim: [] for dim in self.cumul_vars}

            #  get respective dimensions from model
            cvars = {dim: self.model.GetDimensionOrDie(dim) for dim in self.cumul_vars}

            while not self.model.IsEnd(index):
                previous_index = index
                node = self.manager.IndexToNode(index)
                route.append(node)

                for dim_name, dim in cvars.items():
                    val = assignment.Value(dim.CumulVar(index))
                    cum_dim[dim_name].append(val)

                index = assignment.Value(self.model.NextVar(index))
                transit_cost.append(self.model.GetArcCostForVehicle(previous_index, index, 0))

            for dim_name, dim in cvars.items():
                val = assignment.Value(dim.CumulVar(index))
                cum_dim[dim_name].append(val)
                cum_dims[dim_name].append(cum_dim[dim_name])
            if len(route) != 1:
                routes.append(route)
            # else:
            #     print('route', route)
            transit_costs.append(transit_cost)

        return {
            'routes': routes,
            'objective_value': assignment.ObjectiveValue(),
            'transit_costs': transit_costs,
            'cumulative_dimensions': cum_dims,
        }

    @abstractmethod
    def print_solution(self, assignment):
        """Print routing_problems information on console."""
        raise NotImplementedError

    def plot_solution(self):
        """Plot the assigned tours"""
        if 'locations' not in self.data.keys():
            raise RuntimeError('No location data available!')
        if not self.solution:
            raise RuntimeError('No feasible solution provided!')

        cmap = plt.get_cmap("tab20")

        # get routes and locations
        locations = self.data['locations']
        routes = self.solution['routes']

        # remove dummy nodes from plot
        # DEBUGGING: out-comment to plot break nodes
        if self.dummy_indices:
            routes_without_dummies = []
            # careful: this way it only works for 1 break per vehicle!
            # otherwise need to use additional indexing
            for i, r in enumerate(routes):
                r.remove(self.dummy_indices[i])
                routes_without_dummies.append(r)

        # scale arrow sizes by plot scale, indicated by max distance from center
        max_dist_from_zero = np.max(np.abs(locations))
        hw = max_dist_from_zero * 0.025
        hl = hw * 1.2

        # scatter plot of locations
        plt.scatter(locations[:, 0], locations[:, 1], c='k')
        plt.plot(locations[0, 0], locations[0, 1], 'ro')

        # insert arrows indicating routes
        for color_id, route_map in enumerate(routes):
            route = route_map.copy()
            route.append(0)
            for i in range(0, len(route) - 1):
                x1 = locations[route[i], 0]
                x2 = locations[route[i + 1], 0] - x1
                y1 = locations[route[i], 1]
                y2 = locations[route[i + 1], 1] - y1
                c = cmap(color_id)
                plt.arrow(x1, y1, x2, y2,
                          color=c, linestyle='-',
                          head_width=hw, head_length=hl,
                          length_includes_head=True)
        plt.show()

    def plot_search_trajectory(self):
        """Plot value sequence of objective function"""
        plt.plot(self.search_trajectory_callback.buffer)
        plt.xlabel('iterations')
        plt.ylabel('objective value')
        plt.show()

    def get_objective_trajectory(self) -> list:
        return self.search_trajectory_callback.buffer

    def get_solutions_trajectory(self) -> Tuple:
        """Collect the List[List] solutions found"""
        return self.solution_callback.buffer, self.solution_callback.buffer_time

    # def get_runtime_trajectory(self) -> list:
    #     """Collect the List[List] solutions found"""
    #     return self.runtime_log.buffer

    @clockit_return
    def _solve(self, parameters):
        return self.model.SolveWithParameters(parameters)

    @clockit_return
    def _solve_with_assignment(self, parameters, init_assigment):
        self.model.CloseModelWithParameters(parameters)
        initial_solution = self.model.ReadAssignmentFromRoutes(init_assigment, True)
        # print('init_assigment', init_assigment)
        assert is_feasible(init_assigment, self.data, verbose=True)
        if initial_solution is None:
            logger.error(f"Routing status: {STATUS[self.model.status()]}")
            raise RuntimeError(f"provided initial solution is not feasible.")

        # Solve the problem i.e. Improve initial solution
        return self.model.SolveFromAssignmentWithParameters(initial_solution, parameters)

    def solve(self,
              first_solutions_strategy='automatic',
              local_search_strategy='automatic',
              init_solution: List[List] = None,
              time_limit=None,
              solution_limit=None,
              verbose=False,
              log_search=False,
              advanced_search_operators=True,
              **kwargs):
        """

        Args:
            first_solutions_strategy (str): one of
                AUTOMATIC:                  Lets the solver detect which strategy to use
                                            according to the model being solved.
                SAVINGS:                    Savings algorithm (Clarke & Wright)
                CHRISTOFIDES:               Christofides algorithm (actually a variant which does not guarantee the
                                            1.5 factor of the approximation on a metric travelling salesman).
                                            Works on generic vehicle routing_problems models by extending a route until
                                            no nodes can be inserted on it.
                PATH_CHEAPEST_ARC:          Starting from a route "start" node, connect it to the node which produces
                                            the cheapest route segment, then extend the route by iterating on the last
                                            node added to the route.
                PATH_MOST_CONSTRAINED_ARC:  Similar to PATH_CHEAPEST_ARC, but arcs are evaluated with a comparison-based
                                            selector which will favor the most constrained arc first.
                PARALLEL_CHEAPEST_INSERTION:Iteratively build a solution by inserting the cheapest node at its cheapest
                                            position; the cost of insertion is based on the the arc cost function.
                LOCAL_CHEAPEST_INSERTION:   Differs from PARALLEL_CHEAPEST_INSERTION by the node selected for insertion;
                                            here nodes are considered in their order of creation.
                GLOBAL_CHEAPEST_ARC:        Iteratively connect two nodes which produce the cheapest route segment.
                LOCAL_CHEAPEST_ARC:         Select the first node with an unbound successor and connect it to the node
                                            which produces the cheapest route segment.
                FIRST_UNBOUND_MIN_VALUE: 	Select the first node with an unbound successor and connect it to the
                                            first available node.

            local_search_strategy (str): one of
                AUTOMATIC:              Lets the solver select the metaheuristic.
                GREEDY_DESCENT: 	    Accepts improving local search neighbors until a local minimum is reached.
                GUIDED_LOCAL_SEARCH: 	Uses guided local search to escape local minima
                SIMULATED_ANNEALING: 	Uses simulated annealing to escape local minima
                TABU_SEARCH:         	Uses tabu search to escape local minima
                OBJECTIVE_TABU_SEARCH: 	Uses tabu search on the objective value of solution to escape local minima

            init_solution (List[List]): initial solution from which to search
            time_limit (int): Limit in seconds to the time spent in the search.
            solution_limit (int): number of local searches (None for automatic, 1 for only initial solution)
            verbose (bool): verbosity flag
            log_search (bool): log local search steps flag

        Returns:
            The according routes, costs and value of the objective function.
            Returns None when no feasible solution was found
            Returns an info dict with solver status and runtime

        """

        # add search monitor
        self._monitor_search()

        # assign search parameters
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        if advanced_search_operators:
            search_parameters.local_search_operators.use_extended_swap_active = 3
            search_parameters.local_search_operators.use_relocate_neighbors = 3
            search_parameters.local_search_operators.use_cross_exchange = 3
            search_parameters.local_search_operators.use_path_lns = 3
            search_parameters.local_search_operators.use_inactive_lns = 3
            search_parameters.heuristic_close_nodes_lns_num_nodes = 10  # default=5
            search_parameters.improvement_limit_parameters.improvement_rate_coefficient = 550.5
            search_parameters.improvement_limit_parameters.improvement_rate_solutions_distance = 38  # 50
        # logger.info(f'Search Parameters used for search with TL={time_limit}: {search_parameters}')
        if log_search:
            search_parameters.log_search = True  # should search steps be logged?
        if time_limit is not None:
            search_parameters.time_limit.seconds = int(time_limit)  # convert seconds to milliseconds
        if solution_limit is not None:
            search_parameters.solution_limit = solution_limit

        info = {}
        # Solve the problem...
        # ... with initially provided solution
        if init_solution is not None:
            if verbose:
                print('init_sol', init_solution)
                print('Start solving routing problem with initially constructed solution...')

            # add local search meta-heuristic
            search_parameters.local_search_metaheuristic = (getattr(
                routing_enums_pb2.LocalSearchMetaheuristic, local_search_strategy.upper()))

            assignment, time_elapsed = self._solve_with_assignment(parameters=search_parameters,
                                                                   init_assigment=init_solution)
        # ... or from scratch
        else:
            # Setting first solution heuristic (e.g. cheapest addition)
            search_parameters.first_solution_strategy = (getattr(
                routing_enums_pb2.FirstSolutionStrategy, first_solutions_strategy.upper()))

            # add local search meta-heuristic
            search_parameters.local_search_metaheuristic = (getattr(
                routing_enums_pb2.LocalSearchMetaheuristic, local_search_strategy.upper()))

            self.model.CloseModelWithParameters(search_parameters)

            if verbose:
                print('Start solving routing problem...')

            assignment, time_elapsed = self._solve(parameters=search_parameters)

        status = self._model_status()

        if assignment:
            info['status'] = status
            info['time_elapsed'] = time_elapsed
            solution = self.parse_assignment(assignment)
            info['running_costs'] = self.get_objective_trajectory()
            info['running_solutions'] = self.get_solutions_trajectory()[0]
            info['running_times'] = self.get_solutions_trajectory()[1]
            self.solution = solution.copy()
            if verbose:
                print(f'finished. \nSolver status: {status}')
                self.print_solution(assignment)
        else:
            if verbose:
                print(f'No feasible solution found! \nSolver status: {status}')
            info['status'] = status
            info['running_solutions'] = None
            info['running_times'] = None
            solution = None

        return solution, info

    def close(self):
        del self.manager
        del self.model
        del self.callbacks
        del self.search_trajectory_callback


class TSPSolver(RoutingSolver):
    """Standard (symmetric) Traveling Salesman Problem"""

    def __init__(self):
        super(TSPSolver, self).__init__()
        raise NotImplementedError

    def add_transit_dimension(self, maximum_cap=1000, name='Transit', **kwargs):
        """Add transit dimension"""

        # travel distances/times
        transit_callback_index = self.model.RegisterTransitCallback(self.callbacks['transits'])

        self.model.AddDimension(
            transit_callback_index,
            slack_max=0,  # null slack
            capacity=maximum_cap,  # Maximum distance per vehicle
            fix_start_cumul_to_zero=True,
            name=name)

        self.cumul_vars += [name]

        return transit_callback_index

    def create_model(self, data, **kwargs):
        """Creates model and adds data, cost evaluators, dimensions and constraints.

        data format:
            n (int): number of nodes including depot
            k (int): number of available vehicles
            depot (int): index of depot (can be any node for TSP)
            distance_matrix (dict or array): distances from each node to every other node
            locations (array): x,y coordinates of every node (optional, for plotting only)

        """

        self.data = data

        # initialize index manager
        self.manager = pywrapcp.RoutingIndexManager(self.data['n'], self.data['k'], self.data['depot'])
        # initialize model
        self.model = pywrapcp.RoutingModel(self.manager)

        # create data callbacks
        self.create_callbacks()

        # add respective dimensions to objective function
        transit_cb = self.add_transit_dimension(**kwargs)

        # set cost evaluator
        self.model.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    def create_callbacks(self):
        """Creates all necessary data callbacks"""

        def transit_callback(from_index, to_index):
            """Returns the transit cost between the two nodes (e.g. distance or time)."""
            # Convert from routing_problems variable Index to distance matrix NodeIndex.
            from_node = self.manager.IndexToNode(from_index)
            to_node = self.manager.IndexToNode(to_index)
            return self.data['distance_matrix'][from_node][to_node]

        self.callbacks['transits'] = transit_callback

    def print_solution(self, assignment):
        """Prints assignment on console."""
        print('Objective: {} miles'.format(assignment.ObjectiveValue()))
        index = self.model.Start(0)
        plan_output = 'Route for vehicle 0:\n'
        route_distance = 0
        while not self.model.IsEnd(index):
            plan_output += ' {} ->'.format(self.manager.IndexToNode(index))
            previous_index = index
            index = assignment.Value(self.model.NextVar(index))
            route_distance += self.model.GetArcCostForVehicle(previous_index, index, 0)
        plan_output += ' {}\n'.format(self.manager.IndexToNode(index))
        print(plan_output)
        plan_output += 'Route distance: {}miles\n'.format(route_distance)


class CVRPSolver(RoutingSolver):
    """Capacitated Vehicle Routing Problem"""

    def __init__(self):
        super(CVRPSolver, self).__init__()

    @staticmethod
    def convert_instance(data: CVRPInstance, is_normed: bool = True, precision: int = int(1e4)) -> GORTInstance:
        """Convert RPInstance of CVRP to GORTInstance."""
        n = data.graph_size
        k = CVRP_DEFAULTS[n - 1][0] if data.type == "uniform" else n-1  # "uchoa" or data.type[:2] == "XE"
        # k = CVRP_DEFAULTS[n][0]
        precision = precision if is_normed else 1  # correct if data is already un-scaled
        # print('precision', precision)
        locs = data.coords if isinstance(data.coords, np.ndarray) else data.coords.numpy()
        # print('locs BEFORE', locs[:5])
        # print('(np.round(locs * precision))[:5]', (np.round(locs * precision))[:5])
        locs = (locs * precision).astype(int) if not data.type == "Golden" else locs * precision
        # print('locs', locs[:5])
        demands = data.node_features[:, data.constraint_idx[0]]
        # print('demands BEFORE', demands[:80])
        # print('(demands * precision)', (demands * precision))
        # print('np.round(demands * precision)', np.round(demands * precision))
        # demands = (np.ceil(demands * precision)).astype(int)
        demands = (demands * precision).astype(int)
        # print('demands[:5]', demands[:5])
        assert len(locs) == len(demands) == n
        cap = int(data.vehicle_capacity * precision) if is_normed else data.original_capacity * precision
        # print('cap', cap)
        return GORTInstance(
            depot_idx=int(data.depot_idx[0]),
            n=n,
            k=k,
            locations=locs.tolist(),
            demands=demands.tolist(),
            capacity=[cap] * k,
            dist_mat=calculate_distances(locations=locs, distance_metric=l2_distance),
        )

    def add_transit_dimension(self, maximum_cap: int = int(1e6), name: str = "Transit", weight: int = 1, **kwargs):
        """Add transit dimension"""

        # travel distances/times
        transit_callback_index = self.model.RegisterTransitCallback(self.callbacks['transits'])

        self.model.AddDimension(
            transit_callback_index,
            slack_max=0,  # null slack
            capacity=maximum_cap,  # Maximum distance per vehicle
            fix_start_cumul_to_zero=True,
            name=name)

        self.cumul_vars += [name]

        dim = self.model.GetDimensionOrDie(name)
        dim.SetGlobalSpanCostCoefficient(weight)

        return transit_callback_index

    def add_capacity_dimension(self, name: str = "Capacity", weight: int = 0, **kwargs):
        """Add customer demand dimension"""
        demand_callback_index = self.model.RegisterUnaryTransitCallback(self.callbacks['demands'])

        assert len(self.data.capacity) == self.data.k
        self.model.AddDimensionWithVehicleCapacity(
            demand_callback_index,
            slack_max=0,  # null capacity slack
            vehicle_capacities=self.data.capacity,  # vehicle maximum capacities
            fix_start_cumul_to_zero=True,
            name=name)

        if weight > 0:
            dim = self.model.GetDimensionOrDie(name)
            dim.SetGlobalSpanCostCoefficient(weight)

        self.cumul_vars += [name]

    def create_model(self, data: GORTInstance, transit_weight: int = 1, **kwargs):
        """Creates model and adds data, cost evaluators, dimensions and constraints.

        data format:
            n (int): number of nodes including depot
            k (int): number of available vehicles
            depot (int): index of depot (default: 0)
            distance_matrix (dict or array): distances from each node to every other node
            demands (list or array): demand of each node (0 for depot)
            vehicle_capacities (list or array): capacity of vehicles (can be homogeneous or heterogeneous)
            locations (array): x,y coordinates of every node (optional, for plotting only)

        """

        self.data = data

        # initialize index manager
        self.manager = pywrapcp.RoutingIndexManager(self.data.n, self.data.k, self.data.depot_idx)
        # initialize model
        self.model = pywrapcp.RoutingModel(self.manager)

        # create data callbacks
        self.create_callbacks()

        # add respective dimensions to objective function
        transit_cb = self.add_transit_dimension(weight=transit_weight, **kwargs)
        self.add_capacity_dimension(**kwargs)

        # set cost evaluator
        self.model.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    def create_callbacks(self):
        """Creates all necessary data callbacks"""

        def transit_callback(from_index, to_index):
            """Returns the transit cost between the two nodes (e.g. distance or time)."""
            # Convert from routing_problems variable Index to distance matrix NodeIndex.
            from_node = self.manager.IndexToNode(from_index)
            to_node = self.manager.IndexToNode(to_index)
            return self.data.dist_mat[from_node][to_node]

        self.callbacks['transits'] = transit_callback

        def demand_callback(from_index):
            """Returns the demand of the node."""
            # Convert from routing_problems variable Index to demands NodeIndex.
            from_node = self.manager.IndexToNode(from_index)
            return self.data.demands[from_node]

        self.callbacks['demands'] = demand_callback

    def print_solution(self, assignment):
        """Prints assignment on console."""
        total_distance = 0
        total_load = 0
        for vehicle_id in range(self.data.k):
            index = self.model.Start(vehicle_id)
            plan_output = 'Route for vehicle {}:\n'.format(vehicle_id)
            route_distance = 0
            route_load = 0
            while not self.model.IsEnd(index):
                node_index = self.manager.IndexToNode(index)
                route_load += self.data.demands[node_index]
                plan_output += ' {0} Load({1}) -> '.format(node_index, route_load)
                previous_index = index
                index = assignment.Value(self.model.NextVar(index))
                route_distance += self.model.GetArcCostForVehicle(
                    previous_index, index, vehicle_id)
            plan_output += ' {0} Load({1})\n'.format(
                self.manager.IndexToNode(index), route_load)
            plan_output += 'Distance of the route: {}m\n'.format(route_distance)
            plan_output += 'Load of the route: {}\n'.format(route_load)
            print(plan_output)
            total_distance += route_distance
            total_load += route_load
        print('Total distance of all routes: {}m'.format(total_distance))
        print('Total load of all routes: {}'.format(total_load))


class CVRPTWSolver(RoutingSolver):
    """Capacitated Vehicle Routing Problem with Time Windows"""

    def __init__(self):
        super(CVRPTWSolver, self).__init__()
        raise NotImplementedError

    def add_transit_dimension(self, maximum_slack=60, maximum_cap=1440, name="Transit", weight=1,
                              fix_start_cumul_to_zero=False, **kwargs):
        """Add dimension for transit"""

        # travel distances/times
        transit_callback_index = self.model.RegisterTransitCallback(self.callbacks['transits'])

        self.model.AddDimension(
            transit_callback_index,
            slack_max=maximum_slack,  # max allowed waiting time
            capacity=maximum_cap,  # Maximum transit time per vehicle
            fix_start_cumul_to_zero=fix_start_cumul_to_zero,
            name=name)

        # add to cumulative variables index
        self.cumul_vars += [name]

        # set dim weight for objective function
        dim = self.model.GetDimensionOrDie(name)
        dim.SetGlobalSpanCostCoefficient(weight)

        return transit_callback_index

    def add_capacity_dimension(self, name="Capacity", weight=1):
        """Add dimension for capacity"""

        # demands/capacity
        demand_callback_index = self.model.RegisterUnaryTransitCallback(self.callbacks['demands'])

        self.model.AddDimensionWithVehicleCapacity(
            demand_callback_index,
            slack_max=0,  # null capacity slack
            vehicle_capacities=self.data['vehicle_capacities'],  # vehicle maximum capacities
            fix_start_cumul_to_zero=True,
            name=name)

        # add to cumulative variables index
        self.cumul_vars += [name]

        # set dim weight for objective function
        dim = self.model.GetDimensionOrDie(name)
        dim.SetGlobalSpanCostCoefficient(weight)

    def add_hard_time_windows_customer(self, name="Transit"):
        """Add hard time window constraints to customer nodes."""
        dim = self.model.GetDimensionOrDie(name)  # get model time dimension
        for customer_node, customer_time_window in enumerate(self.data["service_time_windows"]):
            if customer_node == 0:  # do not add for depot
                continue
            index = self.manager.NodeToIndex(customer_node)  # convert customer index to global node index
            # set the range / window for the cumulative time on that node
            dim.CumulVar(index).SetRange(customer_time_window[0], customer_time_window[1])

    def add_soft_time_windows_customer(self, name="Transit", early_penalty=0.1, late_penalty=0.5):
        """Add soft time window constraints to customer nodes."""
        dim = self.model.GetDimensionOrDie(name)
        for customer_node, customer_time_window in enumerate(self.data["service_time_windows"]):
            if customer_node == 0:  # depot
                continue
            index = self.manager.NodeToIndex(customer_node)
            dim.SetCumulVarSoftLowerBound(index, int(customer_time_window[0]), int(early_penalty * 100))
            dim.SetCumulVarSoftUpperBound(index, int(customer_time_window[1]), int(late_penalty * 100))

    def add_soft_time_windows_with_waiting_customer(self, name="Transit", early_penalty=0.1, late_penalty=0.5):
        """Add soft time window constraints to customer nodes."""
        dim = self.model.GetDimensionOrDie(name)
        for customer_node, customer_time_window in enumerate(self.data["service_time_windows"]):
            if customer_node == 0:  # depot
                continue
            index = self.manager.NodeToIndex(customer_node)
            # b of hard TW is limit of horizon to allow for soft TW
            dim.CumulVar(index).SetRange(customer_time_window[0], self.data['horizon'])
            dim.SetCumulVarSoftLowerBound(index, int(customer_time_window[0]), int(early_penalty * 100))
            dim.SetCumulVarSoftUpperBound(index, int(customer_time_window[1]), int(late_penalty * 100))

    def add_hard_time_windows_vehicle(self, name="Transit"):
        """Add hard time window constraints to vehicle nodes."""

        dim = self.model.GetDimensionOrDie(name)
        for vehicle_node, vehicle_time_window in enumerate(self.data["shift_time_windows"]):
            start_index = self.model.Start(vehicle_node)
            stop_index = self.model.End(vehicle_node)
            try:
                dim.CumulVar(start_index).SetRange(vehicle_time_window[0], vehicle_time_window[1] - 1)
                dim.CumulVar(stop_index).SetRange(vehicle_time_window[0] + 1, vehicle_time_window[1])
            except Exception as e:
                print('fix_start_cumul_to_zero of Transit dimension'
                      ' has to be set to False to allow for later start times')
                raise RuntimeError(e)

        for i in range(len(self.data["shift_time_windows"])):
            self.model.AddVariableMinimizedByFinalizer(
                dim.CumulVar(self.model.End(i)))

    def add_soft_time_windows_vehicle(self, name="Transit", penalty_coefficient=0.5):
        """Add soft time window constraints to vehicle nodes."""
        p = int(penalty_coefficient * 100)
        dim = self.model.GetDimensionOrDie(name)
        for vehicle_node, vehicle_time_window in enumerate(self.data["shift_time_windows"]):
            start_index = self.model.Start(vehicle_node)
            stop_index = self.model.End(vehicle_node)
            dim.SetCumulVarSoftLowerBound(start_index, vehicle_time_window[0], p)
            dim.SetCumulVarSoftUpperBound(stop_index, vehicle_time_window[1], p)

    def create_model(self,
                     data,
                     transit_weight=1,
                     capacity_weight=1,
                     max_waiting_time=30,
                     customer_soft_tw=False,
                     customer_early_penalty=0.1,
                     customer_late_penalty=0.5,
                     vehicle_soft_tw=False,
                     vehicle_soft_penalty=0.5,
                     wait_until_ready=False,
                     allow_late_start=False,
                     **kwargs):
        """Creates model and adds data, cost evaluators, dimensions and constraints

        Args:
            data (dict): dictionary with problem data, including
                            n: number of nodes (customers + depot)
                            k: number of vehicles
                            depot: index of depot
                            distance_matrix: distances/times for transit
                            demands: demand for each customer node, 0 for depot
                            vehicle_capacities: capacity for each vehicle
                            service_durations: time duration of services at customers
                            service_time_windows: customer time windows for service
                            shift_time_windows: time windows of vehicle working shifts
                            horizon: full service time window
                            locations (optional): node coordinates for plotting
            transit_weight (int): weight of transit dimension in objective function
            capacity_weight (int): weight of capacity dimension in objective function
            max_waiting_time (int): maximum allowed waiting time of vehicles
            customer_soft_tw (bool): use soft customer time windows flag
            customer_early_penalty (int): penalty coefficient for soft customer time window when too early
            customer_late_penalty (int): penalty coefficient for soft customer time window when too late
            vehicle_soft_tw (bool): use soft vehicle (shift) time windows flag
            vehicle_soft_penalty (int): penalty coefficient for soft vehicle time window
            wait_until_ready (bool): wait until ready time even in case of soft TW
            allow_late_start (bool): allow vehicles to start late from depot
            **kwargs: additional keyword arguments

        """

        self.data = data

        # initialize index manager
        self.manager = pywrapcp.RoutingIndexManager(self.data['n'], self.data['k'], self.data['depot'])
        # initialize model
        self.model = pywrapcp.RoutingModel(self.manager)

        # create data callbacks
        self.create_callbacks()

        # add respective dimensions to objective function
        transit_cb = self.add_transit_dimension(maximum_slack=max_waiting_time,
                                                weight=transit_weight,
                                                fix_start_cumul_to_zero=(not allow_late_start),
                                                **kwargs)
        self.add_capacity_dimension(weight=capacity_weight)

        # customer time windows
        if customer_soft_tw and wait_until_ready:
            self.add_soft_time_windows_with_waiting_customer(early_penalty=customer_early_penalty,
                                                             late_penalty=customer_late_penalty)
        else:
            if customer_soft_tw:
                self.add_soft_time_windows_customer(early_penalty=customer_early_penalty,
                                                    late_penalty=customer_late_penalty)
            else:
                self.add_hard_time_windows_customer()

        # vehicle time windows
        if vehicle_soft_tw:
            self.add_soft_time_windows_vehicle(penalty_coefficient=vehicle_soft_penalty)
        else:
            self.add_hard_time_windows_vehicle()

        # set cost evaluator (transit dimension)
        self.model.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    def create_callbacks(self):
        """Creates all necessary data callbacks"""

        def transit_callback(from_index, to_index):
            """Returns the transit cost between the two nodes (e.g. distance or time)."""
            # Convert from routing_problems variable Index to distance matrix NodeIndex.
            from_node = self.manager.IndexToNode(from_index)
            to_node = self.manager.IndexToNode(to_index)
            # aggregated time callback of travel time and service time
            return self.data['distance_matrix'][from_node][to_node] + self.data['service_durations'][from_node]

        self.callbacks['transits'] = transit_callback

        def demand_callback(from_index):
            """Returns the demand of the node."""
            # Convert from routing_problems variable Index to demands NodeIndex.
            from_node = self.manager.IndexToNode(from_index)
            return self.data['demands'][from_node]

        self.callbacks['demands'] = demand_callback

    def print_solution(self, assignment):
        """Prints assignment on console."""
        time_dimension = self.model.GetDimensionOrDie('Transit')
        total_time = 0
        total_load = 0
        for vehicle_id in range(self.data['k']):
            index = self.model.Start(vehicle_id)
            plan_output = 'Route for vehicle {}:\n'.format(vehicle_id)
            route_load = 0
            while not self.model.IsEnd(index):
                # cap
                node_index = self.manager.IndexToNode(index)
                route_load += self.data['demands'][node_index]
                # time
                time_var = time_dimension.CumulVar(index)

                # stdout
                plan_output += '{0} Time({1},{2}) Load({3})-> '.format(
                    self.manager.IndexToNode(index),
                    assignment.Min(time_var),
                    assignment.Max(time_var),
                    route_load)

                index = assignment.Value(self.model.NextVar(index))

            time_var = time_dimension.CumulVar(index)
            # stdout
            plan_output += '{0} Time({1},{2}) Load({3})\n'.format(
                self.manager.IndexToNode(index),
                assignment.Min(time_var),
                assignment.Max(time_var),
                route_load)
            plan_output += 'Time of the route: {}min\n'.format(
                assignment.Min(time_var))
            plan_output += 'Load of the route: {}\n'.format(route_load)
            print(plan_output)

            total_time += assignment.Min(time_var)
            total_load += route_load
        print('Total time of all routes: {}min'.format(total_time))
        print('Total load of all routes: {}'.format(total_load))


# def convert_data(data,
#                  problem,
#                  integer_precision=1e4,
#                  state_kwargs={},
#                  **kwargs):
#     """Convert the sampled data to a format that GORT can work with
#
#     Args:
#         data: data of one problem instance
#         problem: problem name (CVRP / CVRPTW)
#         integer_precision: precision at which to convert floats to integers
#         state_kwargs: additional kw arguments for the problem state (needed for consistent evaluation)
#         **kwargs:
#
#     Returns:
#         converted data (dict)
#
#     """
#     raise NotImplementedError
#     new_data = {}
#
#     # prepare locations
#     depot_loc = data['depot_loc'].cpu().numpy()
#     node_loc = data['node_loc'].cpu().numpy()
#     locs = np.vstack((depot_loc, node_loc))
#     # scale locations to integers
#     locs_int = (locs * integer_precision).astype(np.int)
#     new_data['locations'] = locs_int
#     # calculate distance matrix
#     new_data['distance_matrix'] = calculate_distances(locs_int)
#
#     # prepare demand
#     demand = data['demand'].cpu().numpy()
#     cap = int(data['capacity'])
#     demand = (demand * cap).astype(np.int8)  # rescale from [0, 1]
#     new_data['demands'] = [0] + demand.tolist()  # add 0 demand for depot
#
#     # additional attributes
#     n = locs.shape[0]
#     k = n-1
#     if 'max_k_factor' in state_kwargs.keys():
#         k = int(np.ceil(k * state_kwargs['max_k_factor']))
#
#     new_data['n'] = n
#     new_data['k'] = k
#     new_data['depot'] = 0
#     new_data['vehicle_capacities'] = [cap] * k
#
#     # times and durations for CVRPTW
#     if problem.upper() == 'CVRPTW':
#         k = n   # this guarantees feasibility
#         new_data['k'] = k
#         new_data['vehicle_capacities'] = [cap] * k
#         sw = int(data['service_window'] * integer_precision)
#         # vehicle shifts
#         shift_tw = [0, sw]
#         new_data['shift_time_windows'] = [shift_tw]*k
#         # service durations
#         durations = data['durations'].cpu().numpy()
#         durations = durations.astype(np.int64) * integer_precision
#         new_data['service_durations'] = [0] + durations.tolist()
#         # time windows
#         depot_tw = data['depot_tw'].cpu().numpy()
#         node_tw = data['node_tw'].cpu().numpy()
#         tws = np.vstack((depot_tw, node_tw))
#         tws = tws.astype(np.int64) * integer_precision
#         new_data['service_time_windows'] = tws.tolist()
#         new_data['horizon'] = sw
#
#     return new_data


def l1_distance(x1, y1, x2, y2):
    """2d Manhattan distance, returns only integer part"""
    return abs(x1 - x2) + abs(y1 - y2)


def l2_distance(x1, y1, x2, y2):
    """Normal 2d euclidean distance."""
    return np.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def calculate_distances(locations, distance_metric=None, round_to_int=True):
    """Calculate distances between locations as matrix.
    If no distance_metric is specified, uses l2 euclidean distance"""
    metric = l2_distance if distance_metric is None else distance_metric

    num_locations = len(locations)
    matrix = {}

    for from_node in range(num_locations):
        matrix[from_node] = {}
        for to_node in range(num_locations):
            x1 = locations[from_node][0]
            y1 = locations[from_node][1]
            x2 = locations[to_node][0]
            y2 = locations[to_node][1]
            if round_to_int:
                matrix[from_node][to_node] = int(round(metric(x1, y1, x2, y2), 0))
            else:
                matrix[from_node][to_node] = metric(x1, y1, x2, y2)

    return matrix


class ParallelSolver:
    """Parallelization wrapper for RoutingSolver based on multi-processing pool."""

    def __init__(self,
                 problem: str,
                 time_limit: Union[int, float],
                 solver_args: Optional[Dict] = None,
                 num_workers: int = 1,  # to process instances in batches
                 search_workers: int = 1,  # currently not implemented
                 int_prec: int = 10000,
                 ):
        self.problem = problem.upper()
        self.solver_cl = self._get_solver_class(self.problem)
        self.solver_args = solver_args if solver_args is not None else {}
        self.time_limit = time_limit
        self.int_prec = int_prec
        if num_workers > os.cpu_count():
            warnings.warn(f"num_workers > num logical cores! This can lead to "
                          f"decrease in performance if env is not IO bound.")
        self.num_workers = num_workers

    @staticmethod
    def _get_solver_class(problem: str):
        if problem == "CVRP":
            return CVRPSolver
        else:
            raise ValueError(f"unknown problem: '{problem}'")

    @staticmethod
    def _solve(params: Tuple):
        """
        params:
            solver_cl: RoutingSolver.__class__
            data: GORTInstance
            solver_args: Dict
        """
        solver_cl, data, solver_args, time_limit, init_sol = params
        solver = solver_cl()
        solver.create_model(data)
        solution, info = solver.solve(time_limit=time_limit, **solver_args)
        solver.close()
        return [solution, info]

    @staticmethod
    def _solve_with_start(params: Tuple):

        solver_cl, data, solver_args, time_limit, init_sol = params
        solver = solver_cl()
        solver.create_model(data)
        solution, info = solver.solve(init_solution=init_sol, time_limit=time_limit, **solver_args)
        solver.close()
        return [solution, info]

    def solve(self,
              data: List[Union[CVRPInstance, TSPInstance, GORTInstance]],
              distribution: "str",
              time_construct: float = 0.0,
              normed_demands: bool = True,
              init_solution: List[RPSolution] = None) -> List[RPSolution]:

        if not isinstance(data[0], GORTInstance):
            if not normed_demands:
                logger.info(f"Working with original capacity and demand")
            # preprocess for GORT solver
            prep_data = [self.solver_cl.convert_instance(d, is_normed=normed_demands,
                                                         precision=self.int_prec) for d in data]
        else:
            prep_data = data
        print('instance TLs: in GORT solve', [d.time_limit for d in data])

        if init_solution is not None:

            # adjust time_limit by time needed for construction
            self.time_limit = self.time_limit - time_construct if self.time_limit is not None else None
            logger.info(f"Local Search has remaining {self.time_limit} seconds per instance.")

            solve_func = self._solve_with_start
            init_sol_prep_1 = [init_solution[i].solution for i in range(len(prep_data))]
            # need to delete 0s from routes otherwise will be recognised as infeasible sol by ortools
            init_sol_prep = []
            for i in range(len(init_sol_prep_1)):
                if init_sol_prep_1[i] is not None:
                    new_routes = []
                    for route in init_sol_prep_1[i]:
                        if not route[0] == 0 and not route[-1] == 0:
                            new_routes.append(route)
                        else:
                            if not len(route) == 2:  # else is [0,0] route --> ignore empty routes
                                new_routes.append(route[1:-1])
                    init_sol_prep.append(new_routes)
                else:
                    solve_func = self._solve
                    init_sol_prep = [None] * len(prep_data)
        else:
            solve_func = self._solve
            init_sol_prep = [None]*len(prep_data)

        if self.num_workers <= 1:
            if self.time_limit is not None:
                results = list(tqdm(
                    [solve_func((self.solver_cl, prep_data[d], self.solver_args, self.time_limit,
                                 init_sol_prep[d])) for d in range(len(prep_data))],
                    total=len(prep_data)
                ))
            else:
                results = list(tqdm(
                    [solve_func((self.solver_cl, prep_data[d], self.solver_args, data[d].time_limit,
                                 init_sol_prep[d])) for d in range(len(prep_data))],
                    total=len(prep_data)
                ))
            failed = [str(i) for i, res in enumerate(results) if res[0] is None]
            if len(failed) > 0:
                warnings.warn(f"Some instances failed: {failed}")
        else:
            if self.time_limit is not None:
                with Pool(self.num_workers) as pool:
                    results = list(tqdm(
                        pool.imap(
                            solve_func,
                            [(self.solver_cl, prep_data[d], self.solver_args, self.time_limit,
                              init_sol_prep[d]) for d in range(len(prep_data))]
                        ),
                        total=len(prep_data),
                    ))
            else:
                with Pool(self.num_workers) as pool:
                    results = list(tqdm(
                        pool.imap(
                            solve_func,
                            [(self.solver_cl, prep_data[d], self.solver_args, data[d].time_limit,
                              init_sol_prep[d]) for d in range(len(prep_data))]
                        ),
                        total=len(prep_data),
                    ))
            failed = [str(i) for i, res in enumerate(results) if res[0] is None]
            if len(failed) > 0:
                warnings.warn(f"Some instances failed: {failed}")

        # plot instances which failed -> saved in visualisations dir
        SAVE_PATH = os.path.join(os.getcwd(), 'visualisations/failed_')
        # create directory if it doesn't exist
        os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)
        if failed:
            logger.info(f"saving coords of failed instances in dir {SAVE_PATH}")
            for i in failed:
                save_name = distribution + "_instance_" + i
                view = Viewer(locs=data[int(i)].coords, save_dir=SAVE_PATH, gif_naming=save_name)

        # print('data[7]', data[7])
        # print('len results[0][1][running_solutions]', len(results[0][1]['running_solutions']))
        # print('results[0][1][running_times][-5:]', results[0][1]['running_times'][-5:])

        # update running sols
        for r in results:
            if r[1]['running_solutions'] is not None:
                running_sols_updated = []
                for running_sol in r[1]['running_solutions']:
                    # print('running_sol:', running_sol)
                    running_sols_updated.append([route+[0] for route in running_sol])
                r[1]['running_sols_upd'] = running_sols_updated
            else:
                r[1]['running_sols_upd'] = None
            # print("r[1]['running_sols_upd'][0] == r[1]['running_sols_upd'][1]  == r[1]['running_sols_upd'][2]",
            #       r[1]['running_sols_upd'][0] == r[1]['running_sols_upd'][1] == r[1]['running_sols_upd'][2])
            # print("r[1]['running_costs']", r[1]['running_costs'])
        return [
            RPSolution(
                solution=r[0]['routes'] if r[0] is not None else None,
                run_time=r[1]['time_elapsed'] if r[0] is not None else float('inf'),
                problem=self.problem,
                instance=d,
                running_sols=r[1]['running_sols_upd'] if r[1]['running_solutions'] is not None else None,
                running_times=r[1]['running_times'] if r[1]['running_times'] is not None else None,
                #running_costs=r[1]['running_costs'] if r[1]['running_costs'] is not None else None,
            )
            for d, r in zip(data, results)
        ]


# TESTS
# =================================
def _create_data():
    """Stores the data for the problem."""
    rnds = np.random.RandomState(1)
    n = 21
    k = 4
    data = {}
    data['n'] = n
    locs = rnds.uniform(0, 1, size=(n, 2))
    locs[0] = [0.5, 0.5]
    data['locations'] = locs

    dists = calculate_distances(locs * 100)
    data['distance_matrix'] = dists
    data['k'] = k
    data['depot'] = 0

    # CVRP
    data['demands'] = list(np.maximum(rnds.poisson(2, n), [1]))
    data['demands'][0] = 0
    print(data['demands'])
    data['vehicle_capacities'] = [16] * k
    print(data['vehicle_capacities'])

    return data


def _test_tsp():
    data = _create_data()
    data['k'] = 1

    solver = TSPSolver()
    solver.create_model(data)
    solution, info = solver.solve(maximum_cap=1000)

    print(solution)
    print(info)

    solver.plot_solution()
    solver.plot_search_trajectory()


def _test_cvrp():
    data = _create_data()

    solver = CVRPSolver()
    solver.create_model(data)
    solution, info = solver.solve(first_solutions_strategy='Savings',
                                  local_search_strategy='guided_local_search',
                                  time_limit=10,
                                  verbose=True)

    print(solution)
    print(info)

    solver.plot_solution()
    solver.plot_search_trajectory()
