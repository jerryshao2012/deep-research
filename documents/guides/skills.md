# Use skills in Deep Agent

Skills add named capabilities or sets of output instructions to Deep Agent. Use this guide to find an installed skill, apply it with relevant context, and review its result. Screenshots are illustrative, and installed skills vary by deployment.

## How skills work

A skill tells Deep Agent how to handle a particular kind of request. One skill might guide a research workflow, another might produce a structured presentation, and another might apply specialized instructions without requiring the same output format.

Roles determine which actions are appropriate:

- Ordinary users find an available skill, select it for a request, provide source context, and review the result.
- Administrators maintain the deployment's available skills and related configuration. They should verify **Live backend** status before refreshing or uploading skills.

## Find an available skill

1. Open **Skills**.
2. Search by capability, category, or keyword.
3. Read each matching skill's description to confirm its intended use.
4. Select an installed skill that matches the task.

If the expected skill is absent, ask an administrator whether it is installed in this deployment.

![Available Skills panel with skill search and installed capabilities](../assets/screenshots/Skills%20-%20Available%20Skills-20260723-pztn.png)

## Configure skills

Administrators can open **Settings > Configuration > Skills** to manage the skills visible to users.

1. Check **Live backend** status before making changes. If it is missing or disconnected, restore the backend connection first.
2. Use search to narrow the current list.
3. Select the refresh icon to retrieve the latest available skills.
4. Use **Upload Skill** to add a skill supplied for the deployment.
5. Use **Save** when the UI shows pending configuration changes.

After an upload or refresh, confirm that the expected skill appears in the list before directing users to apply it.

![Skills configuration with backend status, search, refresh, and upload controls](../assets/screenshots/Skills%20-%20Configuration_Skills-20260723-pznq.png)

## Apply a skill

The selected request identifies the skill and can reuse recent messages, state files, and uploaded documents from the current thread. Follow the path supported by the deployment:

- If the UI offers confirmation, select the skill, then review and edit the generated request before sending it.
- If selecting the skill submits immediately, update the current thread's messages, state files, and uploaded documents before selection. If the result needs correction, retry with an explicit request naming the skill, required sources, and intended outcome.

Name the intended result explicitly when more than one interpretation is possible. For example, specify the audience, source material, and desired emphasis for study slides.

![Study Slides skill request using recent messages, state files, and an uploaded document](../assets/screenshots/Skills%20-%20Apply%20a%20Skill-20260723-qaar.png)

## Review the result

The result appears in the conversation. Depending on the skill, the run may also add state files for structured content or later reuse.

Check that the result:

- follows the selected skill's purpose;
- uses the intended messages, files, and documents;
- preserves important facts and source constraints;
- is complete enough for its intended audience.

Open any added state files when the conversation points to them. Continue in the conversation with a focused correction if the result needs revision.

![Structured presentation result created by the selected skill](../assets/screenshots/Skills%20-%20Result%20of%20Applying%20a%20Skill-20260723-qaee.png)

## Troubleshooting

| Problem | Action |
| --- | --- |
| Skills list is empty or **Live backend** status is missing | Confirm the backend is running and connected, then select the refresh icon. Ask an administrator to check deployment configuration if status does not recover. |
| An uploaded skill does not appear | Clear active search terms, select the refresh icon, and verify that the upload completed. Use **Save** only if the UI shows pending configuration changes. |
| Output is irrelevant | Check that the correct skill was selected. Before retrying, update current-thread context, or edit the generated request when confirmation is available, and state the desired audience and outcome explicitly. |
| Expected source context is missing | Attach or select the needed uploaded documents or state files, include the relevant recent messages, and submit again. |
| A retry repeats the same problem | Write an explicit request that names the skill, required sources, output goal, and correction from the previous result. |

## Related documentation

- [Use skills from the CLI](../getting-started/usage.md)
- [Author custom skills](../development/extending-the-agent.md)
