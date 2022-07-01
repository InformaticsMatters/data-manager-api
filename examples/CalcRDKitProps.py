#!/usr/bin/env python
"""An example that illustrates how to use the client to run a job that
calculates molecular properties using RDKit.
"""
import os
import time

from dm_api.dm_api import DmApi, DmApiRv


# Token and project id are taken from environment variables...
token: str = os.environ.get('KEYCLOAK_TOKEN')
project_id: str = os.environ.get('PROJECT_ID')
job_input: str = os.environ.get('JOB_INPUT')

if token:
    print('TOKEN OK')
else:
    print('No token provided')
    exit(1)

if project_id:
    print('PROJECT_ID OK')
else:
    print('No project_id provided')
    exit(1)

if job_input:
    print('JOB_INPUT OK')
else:
    print('No job_input provided')
    exit(1)

# The 'ping()' is a handy, simple, API method
# to check the Data Manager is responding.
rv: DmApiRv = DmApi.ping(token)
if rv.success:
    print('API OK')
else:
    print('API not responding')
    exit(1)

# Put some files in a pre-existing DM Project.
# The project is identified by the project_id.
# We simply name the file (or files) and the project-relative
# destination path.
rv = DmApi.put_unmanaged_project_files(token,
                                       project_id=project_id,
                                       project_files=job_input,
                                       project_path='/work')
if rv.success:
    print('FILE UPLOAD OK')
else:
    print('FILE UPLOAD FAILED')
    exit(1)

# Now, run a Job.
# We identify jobs by using a 'collection', 'job' and 'version'
# and then pass variables expected by the Job in a 'variables' block...
spec = {'collection': 'rdkit',
        'job': 'rdkit-molprops',
        'version': '1.0.0',
        'variables': {
            'separator': 'tab',
            'outputFile': 'work/foo.smi',
            'inputFile': 'work/100.smi'
        }}
rv = DmApi.start_job_instance(token,
                              project_id=project_id,
                              name='My Job',
                              specification=spec)
# If successful the DM returns an instance ID
# (the instance identity of our specific Job)
# and a Task ID, which is responsible for running the Job.
if rv.success:
    instance_id = rv.msg['instance_id']
    task_id = rv.msg['task_id']
    print('JOB STARTED. ID=' + instance_id)
else:
    print('JOB FAILED')
    exit(1)

# We can now use the 'task_id' to query the state of the running Job (its instance).
# When we receive 'done' the Job's finished.
iterations = 0
while True:
    if iterations > 10:
        print("TIMEOUT")
        exit(1)
    rv = DmApi.get_task(token, task_id=task_id)
    if rv.msg['done']:
        break
    print('waiting ...')
    iterations += 1
    time.sleep(5)
print('DONE')

# Here we get Files from the project.
# These might be files the Job's created.
rv = DmApi.get_unmanaged_project_file(token,
                                      project_id=project_id,
                                      project_file='foo.smi',
                                      project_path='/work',
                                      local_file='examples/foo.smi')
if rv.success:
    print('DOWNLOAD OK')
else:
    print('DOWNLOAD FAILED')
    print(rv)

# Now, as the Job remains in the DM until deleted
# we tidy up by removing the Job using the instace ID we were given.
rv = DmApi.delete_instance(token, instance_id=instance_id)
if rv.success:
    print('CLEANUP OK')
else:
    print('CLEANUP FAILED')
    print(rv)
    exit(1)
