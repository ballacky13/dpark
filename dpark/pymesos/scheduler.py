import os, sys
import time
import getpass

from process import UPID, Process

from mesos_pb2 import *
from messages_pb2 import *

class Scheduler(object):
    def registered(self, driver, framework_id, masterInfo): pass
    def reregistered(self, driver, masterInfo): pass
    def disconnected(self, driver): pass
    def frameworkMessage(self, driver, message): pass
    def resourceOffers(self, driver, offers): pass
    def offerRescinded(self, driver, offer_id): pass
    def statusUpdate(self, driver, status): pass
    def executorLost(self, driver, executor_id, slave_id, status): pass
    def slaveLost(self, driver, slave_id): pass
    def error(self, driver, message): pass

class SchedulerDriver(object):
    def start(self): pass
    def join(self): pass
    def run(self): pass
    def abort(self): pass
    def stop(self, failover=False): pass
    def reviveOffers(self): pass
    def requestResources(self, requests): pass
    def declineOffer(self, offerId, filters=None): pass
    def launchTasks(self, offerId, tasks, filters=None): pass
    def killTask(self, taskId): pass
    def sendFrameworkMessage(self, executorId, slaveId, data): pass


class MesosSchedulerDriver(Process):
    def __init__(self, sched, framework, master_uri): 
        Process.__init__(self, 'scheduler')
        self.sched = sched
        #self.executor_info = executor_info
        self.master_uri = master_uri
        self.framework = framework
        self.framework.failover_timeout = 100
        self.framework_id = framework.id
        self.master = None

        self.connected = False
        self.aborted = False
        self.savedOffers = {}
        self.savedSlavePids = {}

    def onNewMasterDetectedMessage(self, pid):
        self.master = UPID(pid)
        self.register()

    def onNoMasterDetectedMessage(self, pid):
        self.connected = False
        self.master = None
        self.sched.disconnected()

    def register(self):
        if not self.framework_id.value:
            msg = RegisterFrameworkMessage()
            msg.framework.MergeFrom(self.framework)
        else:
            msg = ReregisterFrameworkMessage()
            msg.framework.MergeFrom(self.framework)
            msg.failover = True
        self.send(self.master, msg)
        # redo after 1 second

    def onFrameworkRegisteredMessage(self, framework_id, master_info):
        self.framework_id = framework_id
        self.framework.id.MergeFrom(framework_id)
        self.connected = True
        self.failover = False
        self.sched.registered(self, framework_id, master_info)

    def onFrameworkReregisteredMessage(self, framework_id):
        assert self.framework_id == framework_id
        self.connected = True
        self.failover = False
        self.sched.reregistered(self, master_info)

    def onResourceOffersMessage(self, offers, pids):
        for offer, pid in zip(offers, pids):
            self.savedOffers.setdefault(offer.id.value, {})[offer.slave_id.value] = UPID(pid)
        self.sched.resourceOffers(self, list(offers))

    def onRescindResourceOfferMessage(self, offer_id):
        self.savedOffers.pop(offer_id.value, None)
        self.sched.offerRescinded(self, offer_id)

    def onStatusUpdateMessage(self, update, pid=''):
        assert self.framework_id == update.framework_id
        self.sched.statusUpdate(self, update.status)
        if not self.aborted and pid:
            reply = StatusUpdateAcknowledgementMessage()
            reply.framework_id.MergeFrom(self.framework_id)
            reply.slave_id.MergeFrom(update.slave_id)
            reply.task_id.MergeFrom(update.status.task_id)
            reply.uuid = update.uuid
            self.send(UPID(pid), reply)

    def onLostSlaveMessage(self, slave_id):
        self.sched.slaveLost(self, slave_id)

    def onExecutorToFrameworkMessage(self, slave_id, executor_id, data):
        self.sched.frameworkMessage(self, msg.slave_id, 
                msg.executor_id, msg.data)

    def onFrameworkErrorMessage(self, message, code=0):
        self.abort()
        self.sched.error(self, code, message)

    def start(self):
        Process.start(self)
        uri = self.master_uri
        if uri.startswith('zk://') or uri.startswith('zoo://'):
            from .detector import MasterDetector
            self.detector = MasterDetector(uri[uri.index('://') + 3:], self)
            self.detector.start()
        else:
            self.onNewMasterDetectedMessage('master@%s' % uri)
        
    def abort(self):
        if self.connected:
            msg = DeactivateFrameworkMessage()
            msg.framework_id.MergeFrom(self.framework_id)
            self.send(self.master, msg)
        Process.abort(self)

    def stop(self, failover=False):
        if self.connected and not failover:
            msg = UnregisterFrameworkMessage()
            msg.framework_id.MergeFrom(self.framework_id)
            self.send(self.master, msg)
        self.abort()

    def requestResources(self, requests):
        msg = ResourceRequestMessage()
        msg.framework_id.MergeFrom(self.framework_id)
        for req in requests:
            msg.requests.add().MergeFrom(req)
        self.send(self.master, msg)

    def reviveOffers(self):
        msg = ReviveOffersMessage()
        msg.framework_id.MergeFrom(self.framework_id)
        self.send(self.master, msg)

    def launchTasks(self, offer_id, tasks, filters=None):
        if not self.connected:
            update = StatusUpdate()
            update.framework_id.MergeFrom(self.framework_id)
            update.status.task_id.MergeFrom(task.task_id)
            update.status.state = TASK_LOST
            update.status.message = 'Master disconnected'
            update.timestamp = time.time()
            update.uuid = ''
            return self.statusUpdate(update)
        
        msg = LaunchTasksMessage()
        msg.framework_id.MergeFrom(self.framework_id)
        msg.offer_id.MergeFrom(offer_id)
        if filters:
             msg.filters.MergeFrom(filters)
        for task in tasks:
            msg.tasks.add().MergeFrom(task)
            pid = self.savedOffers.get(offer_id.value, {}).get(task.slave_id.value)
            if pid:
                self.savedSlavePids[task.slave_id.value] = pid
        self.savedOffers.pop(offer_id.value)
        self.send(self.master, msg)

    def declineOffer(self, offer_id, filters=None):
        pass

    def killTask(self, task_id):
        msg = KillTaskMessage()
        msg.framework_id.MergeFrom(self.framework_id)
        msg.task_id.MergeFrom(task_id)
        self.send(self.master, msg)

    def sendFrameworkMessage(self, executor_id, slave_id, data):
        if not self.connected:
            return
        
        msg = FrameworkToExecutorMessage()
        msg.framework_id.MergeFrom(self.framework_id)
        msg.executor_id.MergeFrom(executor_id)
        msg.slave_id.MergeFrom(slave_id)
        msg.data = data
        
        slave = self.savedSlavePids.get(slave_id.value, self.master) # can not send to slave directly
        self.send(slave, msg)
