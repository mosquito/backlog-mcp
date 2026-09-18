# Where to cut work into tasks

## Cut along outcomes, not along layers

"Store the retry count", "read the retry count in the client" and "show the retry count in the
console" are three layers of one outcome. Filed separately, none of them can be verified alone.
File one task, "retry failed uploads three times", and let it touch three files.

Cut when the parts are useful on their own, or when different people can do them at the same
time.

## Research is its own task

If nobody knows how to do the work yet, the first task is to find out. Give it an outcome that
can be delivered: "Measure which query makes the list page slow", not "look into performance".
Write what was found in the body, then file the tasks the finding makes obvious.

## Bugs

File the report, not the guess: what was done, what happened, what was expected. Add the error
text to the body. When the cause is found, the fix may be several tasks; the report stays as one.

## Size

If a task cannot be finished in a sitting, it is too large. If a task cannot be verified by
someone else, it is too vague. If two tasks always have to be done together, they are one task.

## Keep the plan findable

Give every task of one plan the same tag, and name the plan in each body. Later, `search`
on that tag brings the whole plan back, including the parts that were closed.
