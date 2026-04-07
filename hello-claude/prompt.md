You are running as a scheduled task in the Task Manager system.

Task ID: {{task_id}}
Run time: {{now}}
Storage directory: {{storage_dir}}

## Instructions

1. Use the Bash tool to run: `echo "hello from claude at {{now}}"`
2. Use the Bash tool to run: `date`
3. Write a small file to the storage directory: create `{{storage_dir}}/last_run.txt` containing the current date and a short message confirming the task ran successfully.
4. Print a final summary: "Task {{task_id}} completed successfully."

Keep it brief. No need for explanation — just execute the steps.
