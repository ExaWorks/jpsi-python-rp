
import time
import datetime

import threading as mt

import radical.utils as ru
import radical.pilot as rp

import psi.j as jpsi


# ------------------------------------------------------------------------------
#
PLUGIN_DESCRIPTION = {
    'type'        : 'executor',
    'name'        : 'radical.pilot',
    'class'       : 'AdaptorJobRP',
    'version'     : '0.1',
    'description' : 'this binds the jpsi job API to radical.pilot.'
}


# ------------------------------------------------------------------------------
#
class AdaptorJobRP(jpsi.ExecutorAdaptorBase):

    assert('Session' in dir(rp)), 'incomplete installation'

    # map SAGA states to jpsi states.  Note that SAGA has no notion of
    # `SUBMITTED`.
    _state_map = {
            rp.NEW                        : jpsi.status.NEW,
            rp.TMGR_STAGING_INPUT_PENDING : jpsi.status.QUEUED,
            rp.AGENT_EXECUTING            : jpsi.status.ACTIVE,
            rp.DONE                       : jpsi.status.COMPLETED,
            rp.FAILED                     : jpsi.status.FAILED,
            rp.CANCELED                   : jpsi.status.CANCELED,
    }

    _rev_state_map = {
            jpsi.status.NEW       : rp.NEW,
            jpsi.status.QUEUED    : rp.TMGR_STAGING_INPUT_PENDING,
            jpsi.status.ACTIVE    : rp.AGENT_EXECUTING,
            jpsi.status.COMPLETED : rp.DONE,
            jpsi.status.FAILED    : rp.FAILED,
            jpsi.status.CANCELED  : rp.CANCELED,
    }

    # tuple names for self._jobs
    _JPSI_JOB = 0
    _RP_TASK  = 1


    # --------------------------------------------------------------------------
    #
    def __init__(self,
                 descr   : dict,
                 executor: jpsi.JobExecutor,
                 url     : str
                ) -> None:

        jpsi.ExecutorAdaptorBase.__init__(self, descr, executor, url)

        self._url = ru.Url(url)
        if self._url.schema != 'rp':
            raise ValueError('handle only rp:// URLs, not %s', self._url)


        try:
            self._jobs = dict()     # {job.uid : [JPSI_JOB, RP_TASK]
            self._lock = mt.Lock()

            self._session = rp.Session()

            self._pmgr = rp.PilotManager(session=self._session)
            self._tmgr = rp.TaskManager(session=self._session)

            self._pmgr.register_callback(self._pilot_state_cb)
            self._tmgr.register_callback(self._task_state_cb)

            # this is layer 0, so we just create a dummy pilot
            pd = rp.PilotDescription({'resource': 'local.localhost',
                                      'cores'   : 16,
                                      'runtime' : 60})
            self._pilot = self._pmgr.submit_pilots(pd)
            self._tmgr.add_pilots(self._pilot)

        except Exception:
            self._log.exception('init failed')
            raise


    # --------------------------------------------------------------------------
    #
    def close(self):

        if self._session:
            self._session.close()
            self._session = None
            self._tmgr    = None
            self._pmgr    = None


    # --------------------------------------------------------------------------
    #
    def _pilot_state_cb(self, pilot, rp_state):

        print('-->', pilot.uid, pilot.state)


    # --------------------------------------------------------------------------
    #
    def _task_state_cb(self, task, rp_state):

        # FIXME: use bulk callbacks

        jpsi_uid = task.name
        jpsi_job = self._get_jpsi_job(jpsi_uid)

        ec  = None
        if task.state in rp.FINAL:
            ec = task.exit_code

        self._log.debug('%s --> %s', jpsi_uid, task.state)

        old_state = jpsi_job.status.state
        new_state = self._state_map.get(task.state)

        if not new_state:
            # not an interesting state transition, ignore
            return True

        if old_state != new_state:
            self._executor._advance(jpsi_job, new_state,
                                    update={'exit_code': ec,
                                            'time'     : time.time()})
        return True


    # --------------------------------------------------------------------------
    #
    def submit(self, jobs):

        # derive RP task descriptions and submit them

        jobs  = ru.as_list(jobs)

        for job in jobs:
            job.status.update({'meta_data': {},
                               'exit_code': None,
                               'final'    : False
                              })

        tds   = [self._job_2_descr(job) for job in jobs]
        tasks = self._tmgr.submit_tasks(tds)

        with self._lock:

            # TODO: bulk advance
            for task, job in zip(tasks, jobs):

                self._jobs[job.uid] = [job, task]
                job._set_jex(self._executor)

                job.status.update({'message'  : 'rp task submitted',
                                   'time'     : time.time(),
                                   'native_id': task.uid})
                self._executor._advance(job, jpsi.status.SUBMITTED)


    # --------------------------------------------------------------------------
    #
    def _get_rp_task(self, uid):

        with self._lock:
            return self._jobs[uid][self._RP_TASK]


    # --------------------------------------------------------------------------
    #
    def _get_jpsi_job(self, uid):

        with self._lock:
            return self._jobs[uid][self._JPSI_JOB]


    # --------------------------------------------------------------------------
    #
    def list(self):

        with self._lock:

            return [task.uid for _, task in self._jobs.values()]


    # --------------------------------------------------------------------------
    #
    def _job_2_descr(self, job):

        # FIXME: RP does not support STDIN.  Should we raise if STDIN is
        #        specified?

        from_dict = dict()

        # TODO: use meta data for jpsi uid
        from_dict['name'       ] = job.uid

        from_dict['executable' ] = job.spec.executable
        from_dict['arguments'  ] = job.spec.arguments
        from_dict['environment'] = job.spec.environment
      # from_dict['stdin'      ] = job.spec.stdin_path
        from_dict['stdout'     ] = job.spec.stdout_path
        from_dict['stderr'     ] = job.spec.stderr_path
        from_dict['sandbox'    ] = job.spec.directory

        return rp.TaskDescription(from_dict=from_dict)


    # --------------------------------------------------------------------------
    #
    def job_wait(self, job, states, timeout=None):

        task      = self._get_rp_task(job.uid)
        rp_states = [self._rev_state_map[state] for state in states]

        if timeout and isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()

        task.wait(state=rp_states, timeout=timeout)

        # only return status when target state was reached
        if job.status.state in states:
            return job.status


    # --------------------------------------------------------------------------
    #
    def job_cancel(self, job):

        task = self._get_rp_task(job.uid)
        task.cancel()


# ------------------------------------------------------------------------------

