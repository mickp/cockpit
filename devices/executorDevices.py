## This module handles interacting with the DSP card that sends the digital and
# analog signals that control our light sources, cameras, and piezos. In 
# particular, it effectively is solely responsible for running our experiments.
# As such it's a fairly complex module. 
# 
# A few helpful features that need to be accessed from the commandline:
# 1) A window that lets you directly control the digital and analog outputs
#    of the DSP.
# >>> import devices.dsp as DSP
# >>> DSP.makeOutputWindow()
#
# 2) Create a plot describing the actions that the DSP set up in the most
#    recent experiment profile.
# >>> import devices.dsp as DSP
# >>> DSP._deviceInstance.plotProfile()
#
# 3) Manually advance the SLM forwards some number of steps; useful for when
#    it has gotten offset and is no longer "resting" on the first pattern.
# >>> import devices.dsp as DSP
# >>> DSP._deviceInstance.advanceSLM(numSteps)
# (where numSteps is an integer, the number of times to advance it).

import Pyro4
import time

import depot
import device
import events
import handlers.executor
import handlers.genericHandler
import handlers.genericPositioner
import handlers.imager
import util.threads
import numpy as np
from itertools import chain


class ExecutorDevice(device.Device):
    def __init__(self, name, config={}):
        device.Device.__init__(self, name, config)
        ## Connection to the remote DSP computer
        self.connection = None
        ## Set of all handlers we control.
        self.handlers = set()


    ## Connect to the DSP computer.
    @util.threads.locked
    def initialize(self):
        self.connection = Pyro4.Proxy(self.uri)
        self.connection._pyroTimeout = 6
        self.connection.Abort()


    ## We care when cameras are enabled, since we control some of them 
    # via external trigger. There are also some light sources that we don't
    # control directly that we need to know about.
    def performSubscriptions(self):
        #events.subscribe('camera enable', self.toggleCamera)
        #events.subscribe('light source enable', self.toggleLightHandler)
        events.subscribe(events.USER_ABORT, self.onAbort)
        events.subscribe(events.PREPARE_FOR_EXPERIMENT, self.onPrepareForExperiment)

    ## As a side-effect of setting our initial positions, we will also
    # publish them. We want the Z piezo to be in the middle of its range
    # of motion.
    def makeInitialPublications(self):
        pass

    ## User clicked the abort button.
    def onAbort(self):
        self.connection.Abort()
        # Various threads could be waiting for a 'DSP done' event, preventing
        # new DSP actions from starting after an abort.
        events.publish(events.EXECUTOR_DONE % self.name)


    @util.threads.locked
    def finalizeInitialization(self):
        # Tell the remote DSP computer how to talk to us.
        server = depot.getHandlersOfType(depot.SERVER)[0]
        self.receiveUri = server.register(self.receiveData)
        self.connection.receiveClient(self.receiveUri)


    ## We control which light sources are active, as well as a set of 
    # stage motion piezos. 
    def getHandlers(self):
        result = []
        h = handlers.executor.AnalogDigitalExecutorHandler(
            "DSP", "executor",
            {'examineActions': lambda *args: None,
             'executeTable': self.executeTable,
             'readDigital': self.connection.ReadDigital,
             'writeDigital': self.connection.WriteDigital,
             'getAnalog': self.connection.ReadPosition,
             'setAnalog': self.connection.MoveAbsoluteADU,
             },
            dlines=16, alines=4)

        result.append(h)

        # The takeImage behaviour is now on the handler. It might be better to
        # have hybrid handlers with multiple inheritance, but that would need
        # an overhaul of how depot determines handler types.
        result.append(handlers.imager.ImagerHandler(
            "DSP imager", "imager",
            {'takeImage': h.takeImage}))

        self.handlers = set(result)
        return result


    ## Receive data from the executor remote.
    def receiveData(self, action, *args):
        if action.lower() in ['done', 'dsp done']:
            events.publish(events.EXECUTOR_DONE % self.name)


    def triggerNow(self, line, dt=0.01):
        self.connection.WriteDigital(self.connection.ReadDigital() ^ line)
        time.sleep(dt)
        self.connection.WriteDigital(self.connection.ReadDigital() ^ line)


    ## Prepare to run an experiment.
    def onPrepareForExperiment(self, *args):
        # Ensure remote has the correct URI set for sending data/notifications.
        self.connection.receiveClient(self.receiveUri)


    ## Actually execute the events in an experiment ActionTable, starting at
    # startIndex and proceeding up to but not through stopIndex.
    def executeTable(self, name, table, startIndex, stopIndex, numReps, 
            repDuration):
        # Take time and arguments (i.e. omit handler) from table to generate actions.
        t0 = float(table[startIndex][0])
        actions = [(float(row[0])-t0,) + tuple(row[2:]) for row in table[startIndex:stopIndex]]
        # If there are repeats, add an extra action to wait until repDuration expired.
        if repDuration is not None:
            repDuration = float(repDuration)
            if actions[-1][0] < repDuration:
                # Repeat the last event at t0 + repDuration
                actions.append( (t0+repDuration,) + tuple(actions[-1][1:]) )
        events.publish(events.UPDATE_STATUS_LIGHT, 'device waiting',
                'Waiting for\nDSP to finish', (255, 255, 0))
        self.connection.PrepareActions(actions, numReps)
        events.executeAndWaitFor(events.EXECUTOR_DONE % self.name, self.connection.RunActions)
        events.publish(events.EXPERIMENT_EXECUTION)
        return


        ## Debugging function: set the digital output for the DSP.
    def setDigital(self, value):
        self.connection.WriteDigital(value)



class LegacyDSP(ExecutorDevice):
    import numpy
    ## TODO: test with hardware.
    #        May need to wrap profile digitals and analogs in numpy object.
    def __init__(self, name, config):
        super(self.__class__, self).__init__(name, config)
        self.tickrate = 10 # Number of ticks per ms.
        self._lastAnalogs = []

    def onPrepareForExperiment(self, *args):
        super(self.__class__, self).onPrepareForExperiment(*args)
        self._lastAnalogs = [self.connection.ReadPosition(i) for i in range(4)]
        self._lastDigital = self.connection.ReadDigital()


    ## Receive data from the DSP computer.
    def receiveData(self, action, *args):
        if action.lower() == 'dsp done':
            events.publish(events.EXECUTOR_DONE % self.name)


    ## Actually execute the events in an experiment ActionTable, starting at
    # startIndex and proceeding up to but not through stopIndex.
    def executeTable(self, name, table, startIndex, stopIndex, numReps,
            repDuration):
        # Take time and arguments (i.e. omit handler) from table to generate actions.
        # For the UCSF m6x DSP device, we also need to:
        #  - make the analogue values offsets from the current position;
        #  - convert float in ms to integer clock ticks and ensure digital
        #    lines are not changed twice on the same tick;
        #  - separate analogue and digital events into different lists;
        #  - generate a structure that describes the profile.
        # Start time
        t0 = float(table[startIndex][0])
        # Profiles
        analogs = [ [], [], [], [] ] # A list of lists (one per channel) of tuples (ticks, (analog values))
        digitals = [] # A list of tuples (ticks, digital state)
        # Need to track time of last analog events to workaround a
        # DSP bug later. Also used to detect when events exceed timing
        # resolution
        tLastA = None
        for (t, handler, (darg, aargs)) in table[startIndex:stopIndex]:
            # Convert t to ticks as int while rounding up. The rounding is
            # necessary, otherwise e.g. 10.1 and 10.1999999... both result in 101.
            ticks = int(float(t) * self.tickrate + 0.5)

            # Digital actions - one at every time point.
            if len(digitals) > 0:
                print ticks, digitals[-1][0], '   ', darg, digitals[-1][1]
                if ticks == digitals[-1][0] and darg != digitals[-1][1]:
                    print "TIMING RESOLUTION EXCEEDED"
            digitals.append((ticks, darg))


            # Analogue actions - only enter into profile on change.
            # DSP uses offsets from value when the profile was loaded.
            offsets = map(lambda base, new: base - new, self._lastAnalogs, aargs)
            for offset, a in zip(offsets, analogs):
                if ((len(a) == 0 and offset != 0) or
                        (len(a) > 0 and offset != a[-1][1])):
                    #print tLastA, t
                    a.append((ticks, offset))
                    tLastA = t

        # Work around some DSP bugs:
        # * The action table needs at least two events to execute correctly.
        # * Last action must be digital --- if the last analog action is at the same
        #   time or after the last digital action, it will not be performed.
        # Both can be avoided by adding a digital action that does nothing.
        if len(digitals) == 1 or tLastA >= digitals[-1][0]:
            # Just duplicate the last digital action, one tick later.
            digitals.append( (digitals[-1][0]+1, digitals[-1][1]) )


        actions = [(float(row[0])-t0,) + tuple(row[2:]) for row in table[startIndex:stopIndex]]
        # If there are repeats, add an extra action to wait until repDuration expired.
        if repDuration is not None:
            repDuration = float(repDuration)
            if actions[-1][0] < repDuration:
                # Repeat the last event at t0 + repDuration
                actions.append( (t0+repDuration,) + tuple(actions[-1][1:]) )

        # I think the point of this is just to create a struct with C-aligned fields.
        # If so, we don't need numpy for this, as we only touch the first element
        # in this recarray: could use struct, instead.
        description = np.rec.array(
            None,
            formats="u4, f4, u4, u4, 4u4",
            names=('count', 'clock', 'InitDio', 'nDigital', 'nAnalog'),
            aligned=True, shape=1)

        maxticks = reduce(max, chain(zip(*digitals)[0],
                                     *[(zip(*a) or [[None]])[0] for a in analogs]))
        description['count'] = maxticks
        description['clock'] = 1000. / float(self.tickrate)
        description['InitDio'] = self._lastDigital
        description['nDigital'] = len(digitals)
        description['nAnalog'] = [len(a) for a in analogs]

        # Update records of last positions.
        self._lastDigital = digitals[-1][1]
        # _lastAnalogs[i] - (analogs[last][value] or 0 if no actions for that channel)
        self._lastAnalogs = map(lambda x, y: x - (y[-1:][1:] or 0), self._lastAnalogs, analogs)

        events.publish(events.UPDATE_STATUS_LIGHT, 'device waiting',
                       'Waiting for\nDSP to finish', (255, 255, 0))
        self.connection.profileSet(description.tostring(), digitals, *analogs)
        self.connection.DownloadProfile()
        self.connection.InitProfile(numReps)
        events.executeAndWaitFor(events.EXECUTOR_DONE % self.name, self.connection.RunActions)
        events.publish(events.EXPERIMENT_EXECUTION)