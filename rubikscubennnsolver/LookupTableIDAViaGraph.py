#!/usr/bin/env python3

import cProfile as profile
from rubikscubennnsolver import reverse_steps
from rubikscubennnsolver.LookupTable import (
    LookupTable,
    NoIDASolution,
    pretty_time,
    steps_on_same_face_and_layer,
)
import datetime as dt
import logging
import struct
import subprocess
import sys


log = logging.getLogger(__name__)


class LookupTableIDAViaGraph(LookupTable):

    def __init__(
        self,
        parent,
        filename,
        state_target,
        moves_all,
        moves_illegal,
        linecount,
        max_depth=None,
        filesize=None,
        legal_moves=[],
        prune_tables=[],
        multiplier=None,
    ):
        LookupTable.__init__(self, parent, filename, state_target, linecount, max_depth, filesize)
        self.ida_nodes = {}
        self.recolor_positions = []
        self.recolor_map = {}
        self.nuke_corners = False
        self.nuke_edges = False
        self.nuke_centers = False
        self.min_edge_paired_count = 0
        self.prune_tables = prune_tables
        self.multiplier = multiplier

        for x in moves_illegal:
            if x not in moves_all:
                raise Exception("illegal move %s is not in the list of legal moves" % x)

        if legal_moves:
            self.moves_all = list(legal_moves)
        else:
            self.moves_all = []

            for x in moves_all:
                if x not in moves_illegal:
                    self.moves_all.append(x)

        log.info("%s: moves_all %s" % (self, ",".join(self.moves_all)))
        self.step_index = {}
        for (index, step) in enumerate(self.moves_all):
            self.step_index[step] = index

        COST_LENGTH = 1
        STATE_INDEX_LENGTH = 4
        self.ROW_LENGTH = COST_LENGTH + (STATE_INDEX_LENGTH * len(self.moves_all))

        # Cache the results of steps_on_same_face_and_layer() for all
        # combinations of moves we will see while searching.
        self.steps_on_same_face_and_layer_cache = {}
        self.steps_not_on_same_face_and_layer = {}

        for step1 in self.moves_all + [None]:
            for step2 in self.moves_all:
                if steps_on_same_face_and_layer(step1, step2):
                    self.steps_on_same_face_and_layer_cache[(step1, step2)] = True
                else:
                    self.steps_on_same_face_and_layer_cache[(step1, step2)] = False

                    if step1 not in self.steps_not_on_same_face_and_layer:
                        self.steps_not_on_same_face_and_layer[step1] = []
                    self.steps_not_on_same_face_and_layer[step1].append(step2)

    def get_ida_graph_nodes(self):
        return [pt.ida_graph_node for pt in self.prune_tables]

    def set_ida_graph_nodes(self, ida_graph_nodes):
        for (pt, node) in zip(self.prune_tables, ida_graph_nodes):
            pt.ida_graph_node = node

    def init_ida_graph_nodes(self):
        for pt in self.prune_tables:
            pt.ida_graph_node = pt.state_index()

    def ida_heuristic(self):
        lt_state = []
        cost_to_goal = 0

        for pt in self.prune_tables:
            lt_state.append(pt.ida_graph_node)
            offset = pt.ida_graph_node * self.ROW_LENGTH
            pt_cost_to_goal = pt.ida_graph[offset]

            if pt_cost_to_goal > cost_to_goal:
                cost_to_goal = pt_cost_to_goal

        return (tuple(lt_state), cost_to_goal)

    def ida_search(self, cost_to_here, steps_to_here, threshold, prev_step, prev_ida_graph_nodes):
        """
        https://algorithmsinsight.wordpress.com/graph-theory-2/ida-star-algorithm-in-general/
        """
        self.ida_count += 1

        # save a function call
        #(lt_state, cost_to_goal) = self.ida_heuristic()
        lt_state = []
        cost_to_goal = 0

        for pt in self.prune_tables:
            lt_state.append(pt.ida_graph_node)
            offset = pt.ida_graph_node * self.ROW_LENGTH
            pt_cost_to_goal = pt.ida_graph[offset]

            if pt_cost_to_goal > cost_to_goal:
                cost_to_goal = pt_cost_to_goal

        lt_state = tuple(lt_state)

        # calculate f_cost which is the cost to where we are plus the estimated cost to reach our goal
        f_cost = cost_to_here + cost_to_goal

        #log.info("%s: lt_state %s, cost_to_here %s, cost_to_goal %s, prev_ida_graph_nodes %s" %
        #    (self, ",".join(lt_state), cost_to_here, cost_to_goal, ",".join(prev_ida_graph_nodes)))

        # ================
        # Abort Searching?
        # ================
        if f_cost >= threshold:
            return (f_cost, False)

        # Are we done?
        if cost_to_goal == 0:
            self.ida_nodes[lt_state] = steps_to_here
            return (f_cost, True)

        # If we have already explored the exact same scenario down another branch
        # then we can stop looking down this branch
        explored_cost_to_here = self.explored.get(lt_state, 99)
        if explored_cost_to_here <= cost_to_here:
            return (f_cost, False)
        self.explored[lt_state] = cost_to_here

        skip_other_steps_this_face = None
        prune_tables = self.prune_tables
        next_steps = self.steps_not_on_same_face_and_layer[prev_step]
        step_index = self.step_index

        for step in next_steps:

            # https://github.com/cs0x7f/TPR-4x4x4-Solver/issues/7
            """
            Well, it's a simple technique to reduce the number of nodes accessed.
            For example, we start at a position S whose pruning value is no more
            than maxl, otherwise, S will be pruned in previous searching.  After
            a move X, we obtain position S', whose pruning value is larger than
            maxl, which means that X makes S farther from the solved state.  In
            this case, we won't try X2 and X'.
            --cs0x7f
            """
            if skip_other_steps_this_face is not None:
                if self.steps_on_same_face_and_layer_cache[(skip_other_steps_this_face, step)]:
                    continue
                else:
                    skip_other_steps_this_face = None

            # save a function call
            #self.set_ida_graph_nodes(prev_ida_graph_nodes)
            for (pt, node) in zip(prune_tables, prev_ida_graph_nodes):
                pt.ida_graph_node = node

            # This is the equivalent of calling cube.rotate(step). We advance
            # the pt.ida_graph_node for each prune table based on "step".
            curr_ida_graph_nodes = []
            for pt in self.prune_tables:
                start = (pt.ida_graph_node * self.ROW_LENGTH) + 1 + (step_index[step] * 4)
                end = start + 4
                pt.ida_graph_node = struct.unpack(">L", pt.ida_graph[start:end])[0]

                curr_ida_graph_nodes.append(pt.ida_graph_node)

            (f_cost_tmp, found_solution) = self.ida_search(
                cost_to_here + 1,
                steps_to_here + [step],
                threshold,
                step,
                curr_ida_graph_nodes
            )

            if found_solution:
                return (f_cost_tmp, True)
            else:
                if f_cost_tmp > threshold:
                    skip_other_steps_this_face = step
                else:
                    skip_other_steps_this_face = None

        return (f_cost, False)

    def recolor(self):

        if (
            self.nuke_corners
            or self.nuke_edges
            or self.nuke_centers
            or self.recolor_positions
        ):
            log.info("%s: recolor" % self)
            # self.parent.print_cube()

            if self.nuke_corners:
                self.parent.nuke_corners()

            if self.nuke_edges:
                self.parent.nuke_edges()

            if self.nuke_centers:
                self.parent.nuke_centers()

            for x in self.recolor_positions:
                x_color = self.parent.state[x]
                x_new_color = self.recolor_map.get(x_color)

                if x_new_color:
                    self.parent.state[x] = x_new_color

            # self.parent.print_cube()
            # sys.exit(0)

    def get_best_ida_solution(self):
        min_steps = None
        min_steps_len = None

        # log.info("%s: ida_nodes %s" % (self, self.ida_nodes))
        for (state, steps) in self.ida_nodes.items():
            steps_len = len(steps)

            if min_steps_len is None or steps_len < min_steps_len:
                min_steps_len = steps_len
                min_steps = steps

        return min_steps

    def solve(self, min_ida_threshold=None, max_ida_threshold=99):
        """
        The goal is to find a sequence of moves that will put the cube in a state that is
        in our lookup table
        """

        # uncomment to cProfile solve()
        '''
        pass

    def solve(self, min_ida_threshold=None, max_ida_threshold=99):
        profile.runctx('self.solve_with_cprofile()', globals(), locals())

    def solve_with_cprofile(self, min_ida_threshold=None, max_ida_threshold=99):
        '''

        for pt in self.prune_tables:
            pt.load_ida_graph()

        # If this is a lookup table that is staging a pair of colors (such as U and D)
        # then recolor the cubies accordingly.
        self.pre_recolor_state = self.parent.state[:]
        self.pre_recolor_solution = self.parent.solution[:]
        self.recolor()

        # save cube state
        self.original_state = self.parent.state[:]
        self.original_solution = self.parent.solution[:]

        # Avoiding OLL is done by changing the edge parity from odd to even.
        # The edge parity toggles from odd to even or even to odd with every
        # quarter wide turn. Sanity check that avoiding OLL is possible for
        # this table.
        if self.avoid_oll is not None:
            # log.info("%s: verify we can avoid OLL via moves %s" % (self, " ".join(self.moves_all)))
            for step in self.moves_all:
                if "w" in step and not step.endswith("2"):
                    log.info("%s: has avoid_oll %s" % (self, pformat(self.avoid_oll)))
                    break
            else:
                raise Exception(
                    "%s: has avoid_oll %s but there are no quarter wide turns among moves_all %s"
                    % (self, pformat(self.avoid_oll), " ".join(self.moves_all))
                )

        # Get the intial cube state and cost_to_goal
        self.init_ida_graph_nodes()
        (_state, cost_to_goal) = self.ida_heuristic()

        # The cube is already in the desired state, nothing to do
        '''
        if cost_to_goal == 0:
            log.info("%s: cube state %s is in our lookup table" % (self, state))
            tmp_solution = self.parent.solution[:]
            self.parent.state = self.pre_recolor_state[:]
            self.parent.solution = self.pre_recolor_solution[:]

            for step in tmp_solution[len(self.original_solution) :]:
                self.parent.rotate(step)

            return True
        '''

        # If we are here (odds are very high we will be) it means that the current
        # cube state was not in the lookup table.  We must now perform an IDA search
        # until we find a sequence of moves that takes us to a state that IS in the
        # lookup table.
        if min_ida_threshold is None:
            min_ida_threshold = cost_to_goal

        # If this is the case the range loop below isn't worth running
        if min_ida_threshold >= max_ida_threshold + 1:
            raise NoIDASolution(
                "%s FAILED with range %d->%d"
                % (self, min_ida_threshold, max_ida_threshold + 1)
            )

        start_time0 = dt.datetime.now()
        # log.info("%s: using moves %s" % (self, pformat(self.moves_all)))
        log.info(
            "%s: IDA threshold range %d->%d"
            % (self, min_ida_threshold, max_ida_threshold)
        )
        total_ida_count = 0

        for threshold in range(min_ida_threshold, max_ida_threshold + 1):
            steps_to_here = []
            start_time1 = dt.datetime.now()
            self.ida_count = 0
            self.explored = {}
            self.ida_nodes = {}
            ida_graph_nodes = self.get_ida_graph_nodes()

            self.ida_search(0, steps_to_here, threshold, None, ida_graph_nodes)
            total_ida_count += self.ida_count
            best_solution = self.get_best_ida_solution()

            if best_solution:

                self.parent.state = self.pre_recolor_state[:]
                self.parent.solution = self.pre_recolor_solution[:]

                for step in best_solution:
                    self.parent.rotate(step)

                end_time1 = dt.datetime.now()
                log.info(
                    "%s: IDA threshold %d, explored %d nodes in %s (%s total)"
                    % (
                        self,
                        threshold,
                        self.ida_count,
                        pretty_time(end_time1 - start_time1),
                        pretty_time(end_time1 - start_time0),
                    )
                )
                delta = end_time1 - start_time0
                nodes_per_sec = int(total_ida_count / delta.total_seconds())
                log.info(
                    "%s: IDA explored %d nodes in %s, %d nodes-per-sec"
                    % (self, total_ida_count, delta, nodes_per_sec)
                )
                log.info(
                    "%s: IDA found %d step solution %s"
                    % (self, len(best_solution), " ".join(best_solution))
                )
                self.explored = {}
                self.ida_nodes = {}
                return True
            else:
                end_time1 = dt.datetime.now()
                delta = end_time1 - start_time1
                nodes_per_sec = int(self.ida_count / delta.total_seconds())
                log.info(
                    "%s: IDA threshold %d, explored %d nodes in %s, %d nodes-per-sec"
                    % (
                        self,
                        threshold,
                        self.ida_count,
                        pretty_time(delta),
                        nodes_per_sec,
                    )
                )

        log.info(
            "%s: could not find a solution via IDA with max threshold of %d "
            % (self, max_ida_threshold)
        )
        self.parent.state = self.original_state[:]
        self.parent.solution = self.original_solution[:]
        raise NoIDASolution(
            "%s FAILED with range %d->%d"
            % (self, min_ida_threshold, max_ida_threshold + 1)
        )

    def build_ida_graph(self):
        pt_state_filename = self.filename.replace(".txt", ".pt_state")
        parent = self.parent
        index = 0

        with open(pt_state_filename, "w") as fh_pt_state:
            with open(self.filename, "r") as fh:
                for line in fh:
                    (state, steps_to_solve) = line.rstrip().split(":")
                    steps_to_solve = steps_to_solve.split()
                    cost_to_goal = len(steps_to_solve)
                    steps_to_scramble = reverse_steps(steps_to_solve)

                    # dwalton
                    parent.re_init()
                    for step in steps_to_scramble:
                        parent.rotate(step)

                    self.init_ida_graph_nodes()
                    pt_ida_graph_nodes = self.get_ida_graph_nodes()

                    lt_state = ""

                    for x in pt_ida_graph_nodes:
                        assert x <= 9999999
                        lt_state += f"{x:07d}-"

                    lt_state = lt_state.rstrip("-")

                    fh_pt_state.write(f"{lt_state}:{cost_to_goal}\n")
                    index += 1

                    if index % 10000 == 0:
                        log.info("line: %d" % index)


    def solve_via_c(self, min_ida_threshold=None, max_ida_threshold=99):
        self.init_ida_graph_nodes()

        cmd = ["./ida_search_via_graph",]

        for (index, pt) in enumerate(self.prune_tables):
            cmd.append("--prune-table-%d-filename" % index)
            cmd.append(pt.filename.replace(".txt", ".bin"))

            cmd.append("--prune-table-%d-state" % index)
            cmd.append(str(pt.ida_graph_node))

        cmd.append("--legal-moves")
        cmd.append(",".join(self.moves_all))

        if self.multiplier:
            cmd.append("--multiplier")
            cmd.append(str(self.multiplier))

        log.info("solve_via_c:\n    %s \"%s\"\n" % (" ".join(cmd[0:-1]), cmd[-1]))

        output = subprocess.check_output(cmd).decode("utf-8").splitlines()

        for line in output:
            if line.startswith("SOLUTION:"):
                solution = line.strip().split(":")[1].split()
                for step in solution:
                    self.parent.rotate(step)
                return

        raise Exception("Did not find SOLUTION line in\n%s\n" % "\n".join(output))
