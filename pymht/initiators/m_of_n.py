import numpy as np
import pymht.models.pv as pv
from pymht.pyTarget import Target
from munkres import munkres  # https://github.com/jfrelinger/cython-munkres-wrapper
# import pymunkres  # https://github.com/erikliland/munkres
from scipy.stats import chi2
import logging
import time
# import scipy.optimize.linear_sum_assignment

tracking_parameters = {
    'gate_probability': 0.99,
}
tracking_parameters['gamma'] = chi2(df=2).ppf(tracking_parameters['gate_probability'])

CONFIRMED = 1
PRELIMINARY = 0
DEAD = -1

np.set_printoptions(precision=1, suppress=True)

log = logging.getLogger(__name__)

def _solve_global_nearest_neighbour(delta_matrix, gate_distance=np.Inf, **kwargs):
    try:
        DEBUG = kwargs.get('debug', False)
        # Copy and gating
        if DEBUG: print("delta matrix\n", delta_matrix)
        cost_matrix = np.copy(delta_matrix)
        cost_matrix[cost_matrix > gate_distance] = np.Inf
        if DEBUG: print("cost_matrix\n", cost_matrix)

        # Pre-processing
        valid_matrix = cost_matrix < np.Inf
        if np.all(valid_matrix == False):
            return []
        if DEBUG: print("Valid matrix\n", valid_matrix.astype(int))

        bigM = np.power(10., 1.0 + np.ceil(np.log10(1. + np.sum(cost_matrix[valid_matrix]))))
        cost_matrix[np.logical_not(valid_matrix)] = bigM
        if DEBUG: print("Modified cost matrix\n", cost_matrix)

        validCol = np.any(valid_matrix, axis=0)
        validRow = np.any(valid_matrix, axis=1)
        if DEBUG: print("validCol", validCol)
        if DEBUG: print("validRow", validRow)
        nRows = int(np.sum(validRow))
        nCols = int(np.sum(validCol))
        n = max(nRows, nCols)
        if DEBUG: print("nRows, nCols, n", nRows, nCols, n)

        maxv = 10. * np.max(cost_matrix[valid_matrix])
        if DEBUG: print("maxv", maxv)

        rows = np.arange(nRows)
        cols = np.arange(nCols)
        dMat = np.zeros((n, n)) + maxv
        dMat[np.ix_(rows, cols)] = cost_matrix[np.ix_(validRow, validCol)]
        if DEBUG: print("dMat\n", dMat)

        # Assignment
        preliminary_assignment_matrix = munkres(dMat.astype(np.double))
        preliminary_assignments = [(rowI, np.where(row)[0][0]) for rowI, row in
                                   enumerate(preliminary_assignment_matrix)]
        if DEBUG: print("preliminary assignments ", preliminary_assignments)
        if DEBUG: print("preliminary assignments\n", preliminary_assignment_matrix)

        # Post-processing
        rowIdx = np.where(validRow)[0]
        colIdx = np.where(validCol)[0]
        assignments = []
        for preliminary_assignment in preliminary_assignments:
            row = preliminary_assignment[0]
            col = preliminary_assignment[1]
            if (row >= nRows) or (col >= nCols):
                break
            rowI = rowIdx[row]
            colI = colIdx[col]
            if valid_matrix[rowI, colI]:
                assignments.append((rowI, colI))
        assert all([delta_matrix[a[0], a[1]] <= gate_distance for a in assignments])
        return assignments
    except Exception as e:
        print("#" * 20, "CRASH DEBUG INFO", "#" * 20)
        print("deltaMatrix", delta_matrix.shape, "\n", delta_matrix)
        print("gateDistance", gate_distance)
        print("Valid matrix", valid_matrix.shape, "\n", valid_matrix.astype(int))
        print("validCol", validCol.astype(int))
        print("validRow", validRow.astype(int))
        print("dMat", dMat.shape, "\n", dMat)
        print("preliminary assignments", preliminary_assignments)
        print("rowIdx", rowIdx)
        print("colIdx", colIdx)
        print("assignments", assignments)
        print("#" * 20, "CRASH DEBUG INFO", "#" * 20)
        import time
        time.sleep(0.1)
        raise e


def _initiator_distance(delta_vector, dt, v_max, R):
    movement_scalar = dt * v_max
    d_plus = np.maximum(delta_vector - movement_scalar, np.zeros(2))
    d_minus = np.maximum(-delta_vector - movement_scalar, np.zeros(2))
    d = d_plus + d_minus
    D = np.dot(d.T, np.dot(np.linalg.inv(R + R), d))
    return D


def _merge_targets(targets):
    if len(targets) == 1: return targets[0]

    time = targets[0].time
    scanNumber = None
    x_0 = np.mean(np.array([t.x_0 for t in targets]), axis=0)
    assert x_0.shape == targets[0].x_0.shape
    P_0 = np.mean(np.array([t.P_0 for t in targets]), axis=0)
    assert P_0.shape == targets[0].P_0.shape
    return Target(time, scanNumber, x_0, P_0,
                  measurement=targets[0].measurement,  # TODO: Make a less crude solution
                  # measurementNumber=targets[0].measurementNumber
                  )


def _merge_similar_targets(initial_targets, threshold):
    if not initial_targets: return initial_targets
    targets = []
    used_targets = set()
    for target_index, target in enumerate(initial_targets):
        if target_index not in used_targets:
            distance_to_targets = np.array([np.linalg.norm(target.x_0[0:2] - t.x_0[0:2]) for t in initial_targets])
            close_targets = distance_to_targets < threshold
            close_targets_indices = np.where(close_targets)[0]
            log.debug("Merging " + str(len(close_targets)) + " initial targets to 1")
            selected_targets = [initial_targets[i] for i in close_targets_indices if i not in used_targets]
            merged_target = _merge_targets(selected_targets)
            for i in close_targets_indices:
                used_targets.add(i)
            assert type(merged_target) == type(target)
            targets.append(merged_target)
    return targets


class PreliminaryTrack():
    def __init__(self, state, covariance):
        self.estimates = [state]
        self.covariance = covariance
        self.n = 0
        self.m = 0
        self.predicted_state = None
        self.measurement_index = None

    def __str__(self):
        return "({0:}|{1:})".format(self.m, self.n)

    def __repr__(self):
        return "Estimates: " + ",".join([np.array_str(e) for e in self.estimates]) + str(self)

    def get_speed(self):
        return np.linalg.norm(self.estimates[-1][2:4])

    def predict(self, F, Q):
        self.predicted_state = F.dot(self.estimates[-1])
        self.covariance = F.dot(self.covariance).dot(F.T) + Q

    def mn_analysis(self, M, N):
        m = self.m
        n = self.n
        if m >= M:  # n >= N and m >= M:
            return CONFIRMED
        elif n >= N and m < M:
            return DEAD
        else:
            return PRELIMINARY

    def get_predicted_state_and_clear(self):
        return_value = np.copy(self.predicted_state)
        self.predicted_state = None
        return return_value


class Measurement():
    def __init__(self, value, timestamp):
        self.value = value
        self.timestamp = timestamp
        # self.covariance = pv.R_RADAR()

    def __repr__(self):
        from time import strftime, gmtime
        meas_str = "Measurement: (%.2f, %.2f)" % (self.value[0], self.value[1])
        time_str = "Time: " + strftime("%H:%M:%S", gmtime(self.timestamp))
        return "{" + meas_str + ", " + time_str + "}"


class Initiator():
    def __init__(self, M, N, v_max, C, R,  mergeThreshold=5, **kwargs):
        self.N = N
        self.M = M
        self.C = C
        self.R = R
        self.initiators = []
        self.preliminary_tracks = []
        self.v_max = v_max  # m/s
        self.gamma = tracking_parameters['gamma']
        self.last_timestamp = None
        self.merge_threshold = mergeThreshold  # meter
        log.info("Initiator ready")
        log.debug("Initiator gamma: " + str(self.gamma))

    def getPreliminaryTracksString(self):
        return " ".join([str(e) for e in self.preliminary_tracks])

    def processMeasurements(self, measurement_list):
        tic = time.time()
        log.info("processMeasurements " + str(measurement_list.measurements.shape[0]))
        unused_indices, initial_targets = self._processPreliminaryTracks(measurement_list)
        unused_indices = self._processInitiators(unused_indices, measurement_list)
        self._spawnInitiators(unused_indices, measurement_list)
        self.last_timestamp = measurement_list.time
        initial_targets = _merge_similar_targets(initial_targets, self.merge_threshold)
        log.info("new initial targets " + str(len(initial_targets)))
        toc = time.time() - tic
        log.info("processMeasurements runtime: {:.1f}ms".format(toc * 1000))
        return initial_targets

    def _processPreliminaryTracks(self, measurement_list):
        newInitialTargets = []
        measTime = measurement_list.time
        measurement_array = np.array(measurement_list.measurements, dtype=np.float32)
        log.info("_processPreliminaryTracks " + str(len(self.preliminary_tracks)))

        # Check for something to work on
        n1 = len(self.preliminary_tracks)
        n2 = measurement_array.shape[0]
        n3 = measurement_array.size
        if n1 == 0 or n2 == 0 or n3 == 0:
            return np.arange(n2).tolist(), newInitialTargets

        # Predict position
        dt = measTime - self.last_timestamp
        F = pv.Phi(dt)
        Q = pv.Q(dt)
        for track in self.preliminary_tracks:
            track.predict(F, Q)
        predicted_states = np.array([track.get_predicted_state_and_clear()
                                     for track in self.preliminary_tracks],
                                    ndmin=2, dtype=np.float32)

        # Calculate delta matrix
        delta_matrix = np.ones((n1, n2), dtype=np.float32) * np.Inf
        for i, predicted_state in enumerate(predicted_states):
            predicted_measurement = self.C.dot(predicted_state)
            delta_vector = measurement_array - predicted_measurement
            distance_vector = np.linalg.norm(delta_vector, axis=1)
            P_bar = self.preliminary_tracks[i].covariance
            S = self.C.dot(P_bar).dot(self.C.T) + self.R
            S_inv = np.linalg.inv(S)
            K = P_bar.dot(self.C.T).dot(S_inv)
            self.preliminary_tracks[i].K = K
            nis_vector = np.sum(np.matmul(delta_vector, S_inv) * delta_vector, axis=1)
            inside_gate_vector = nis_vector <= self.gamma
            delta_matrix[i,inside_gate_vector] = distance_vector[inside_gate_vector]

        # Assign measurements
        log.debug("\n"+np.array_str(delta_matrix, max_line_width=120))
        assignments = _solve_global_nearest_neighbour(delta_matrix)

        # Update tracks
        for track_index, meas_index in assignments:
            P_bar = self.preliminary_tracks[track_index].covariance
            K = self.preliminary_tracks[track_index].K
            delta_vector = measurement_array[meas_index] - self.C.dot(predicted_states[track_index])
            filtered_state = predicted_states[track_index] + K.dot(delta_vector)
            P_hat = P_bar - K.dot(self.C).dot(P_bar)
            self.preliminary_tracks[track_index].estimates.append(filtered_state)
            self.preliminary_tracks[track_index].covariance = P_hat
            self.preliminary_tracks[track_index].m += 1
            self.preliminary_tracks[track_index].measurement_index = meas_index

        # Add dummy measurement to un-assigned tracks, and increase covariance
        assigned_track_indices = [assignment[0] for assignment in assignments]
        unassigned_track_indices = [track_index
                                    for track_index in range(len(self.preliminary_tracks))
                                    if track_index not in assigned_track_indices]
        for track_index in unassigned_track_indices:
            self.preliminary_tracks[track_index].estimates.append(
                predicted_states[track_index])

        # Increase all N
        for track in self.preliminary_tracks:
            track.n += 1

        log.debug("Preliminary tracks "+self.getPreliminaryTracksString())
        removeIndices = []
        for track_index, track in enumerate(self.preliminary_tracks):
            track_status = track.mn_analysis(self.M, self.N)
            track_speed = track.get_speed()
            if track_speed > self.v_max*1.5:
                log.warning("Removing TOO FAST track ({0:6.1f} m/s) i={1:}".format(track_speed, track_index) +"\n"+ repr(track))
                removeIndices.append(track_index)
            elif track_status == DEAD:
                log.debug("Removing DEAD track " + str(track_index))
                removeIndices.append(track_index)
            elif track_status == CONFIRMED:
                log.debug("Removing CONFIRMED track " + str(track_index))
                new_target = Target(measTime,
                                    None,
                                    np.array(track.estimates[-1]),
                                    track.covariance,
                                    measurementNumber=track.measurement_index + 1,
                                    measurement=measurement_array[track.measurement_index])
                log.debug("Spawning new (initial) Target: " + str(new_target)
                          + " Covariance:\n" + np.array_str(track.covariance))
                newInitialTargets.append(new_target)
                removeIndices.append(track_index)

        for i in reversed(removeIndices):
            self.preliminary_tracks.pop(i)
        if removeIndices:
            log.debug(self.getPreliminaryTracksString())

        used_indices = [assignment[1] for assignment in assignments]
        unused_indices = [index
                          for index in np.arange(n2)
                          if index not in used_indices]
        return unused_indices, newInitialTargets

    def _processInitiators(self, unused_indices, measurement_list):
        log.debug("_processInitiators " + str(len(self.initiators)))
        measTime = measurement_list.time
        measurementArray = np.array(measurement_list.measurements, ndmin=2, dtype=np.float32)
        n1 = len(self.initiators)
        n2 = len(unused_indices)
        if n1 == 0 or n2 == 0:
            return unused_indices


        #TODO: Improve runtime of this section. It takes about 97% of m/n runtime
        unusedMeasurementArray = measurementArray[unused_indices]
        initiatorArray = np.array([i.value for i in self.initiators], ndmin=2, dtype=np.float32)
        deltaTensor = np.empty((n1, n2, 2))
        for i in range(n1):
            deltaTensor[i] = unusedMeasurementArray - initiatorArray[i]
        distance_matrix = np.linalg.norm(deltaTensor, axis=2)


        dt = measTime - self.initiators[0].timestamp
        gate_distance = (self.v_max * dt)
        log.info("Gate distance {0:.1f}".format(gate_distance))

        assignments = _solve_global_nearest_neighbour(distance_matrix, gate_distance)
        assigned_local_indices = [assignment[1] for assignment in assignments]
        used_indices = [unused_indices[j] for j in assigned_local_indices]
        unused_indices = [i for i in unused_indices if i not in used_indices]
        unused_indices.sort()
        assert len(unused_indices) == len(set(unused_indices))
        self.__spawn_preliminary_tracks(unusedMeasurementArray, assignments, measTime)
        return unused_indices

    def _spawnInitiators(self, unused_indices, measurement_list):
        log.debug("_spawnInitiators " + str(len(unused_indices)))
        time = measurement_list.time
        measurement_array = measurement_list.measurements
        self.initiators = [Measurement(measurement_array[index], time)
                           for index in unused_indices]

    def __spawn_preliminary_tracks(self, unusedMeasurementArray, assignments, measTime):
        log.info("__spawn_preliminary_tracks " + str(len(assignments)))

        for initiator_index, measurement_index in assignments:
            delta_vector = unusedMeasurementArray[measurement_index] - self.initiators[initiator_index].value
            dt = measTime - self.initiators[initiator_index].timestamp
            velocity_vector = delta_vector / dt
            speed = np.linalg.norm(velocity_vector)
            if speed > self.v_max*1.5:
                log.warning("Initiator speed to high {0:6.1f} m/s".format(speed) +
                            "\n" + str(delta_vector))
            x0 = np.hstack((unusedMeasurementArray[measurement_index], velocity_vector))
            track = PreliminaryTrack(x0, pv.P0)
            self.preliminary_tracks.append(track)


if __name__ == "__main__":
    import pymht.utils.simulator as sim
    import pymht.models.pv as model

    np.set_printoptions(precision=1, suppress=True)

    # print("Test 1")
    # deltaMatrix = np.array([[5., 2.], [np.Inf, np.Inf]])
    # print("test deltaMatrix\n", deltaMatrix)
    # assignment = _solve_global_nearest_neighbour(deltaMatrix, debug=True)
    # print("test assignment", assignment)

    print("Test 2")
    seed = 1254
    nTargets = 2
    p0 = np.array([0., 0.])
    radarRange = 5500  # meters
    meanSpeed = 10  # gausian distribution
    P_d = 1.0
    sigma_Q = pv.sigmaQ_true

    sim.seed_simulator(seed)

    initialTargets = sim.generateInitialTargets(nTargets, p0, radarRange, P_d, sigma_Q)

    nScans = 4
    timeStep = 0.7
    simTime = nScans * timeStep
    simList = sim.simulateTargets(initialTargets, simTime, timeStep, model)

    lambda_phi = 4e-6
    scanList = sim.simulateScans(simList, timeStep, model.C_RADAR, model.R_RADAR(0),
                                 lambda_phi, radarRange, p0)

    N_checks = 4
    M_required = 2

    v_max = 17
    initiator = Initiator(M_required, N_checks, v_max, pv.C_RADAR, pv.R_RADAR(), debug=False)

    for scanIndex, measurementList in enumerate(scanList):
        print("Scan index", scanIndex)
        # print(measurementList)
        initialTargets = initiator.processMeasurements(measurementList)
        if initialTargets:
        # print(scanIndex, end="\t")
            print(*initialTargets, sep="\n", end="\n\n")
        # print(*initialTargets, se)
        # else:
        # print(scanIndex, [], sep="\t")

        print("-" * 50)
