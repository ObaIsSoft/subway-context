# Releasing subway-context

Releases start only by hand. Merging a pull request never publishes anything.

## The three levels

- **Private**: a production run waiting for approval. The draft GitHub release exists, but nothing is on npm.
- **Preview**: published to npm under `next` and as a GitHub pre-release. Only people who ask for it get it.
- **Production**: published to npm under `latest` and as a normal GitHub release. This is what a normal install gets.

## How to release

1. Open the repository's **Actions** tab.
2. Pick the **Release** workflow.
3. Click **Run workflow**.
4. Pick the branch and the channel.
5. Click **Run workflow** again to start it.

Preview runs can start from any branch. Production runs only from `main`. A production run pauses at the `release` environment. Approve it under **Review deployments** (also available in the GitHub mobile app).

## Preview versions

Previews are numbered `BASE-rc.N`, for example `1.3.4-rc.1`, then `1.3.4-rc.2`. npm never lets a version number be reused, so every preview needs a new number.

## One-time setup

1. In GitHub, open **Settings**, then **Environments**. Create an environment named `release`.
   - Set **Required reviewers** to the owner.
   - Under **Deployment branches**, choose **Selected branches** and add `main`.
2. Add a trusted publisher on npm:

   ```
   npm trust github subway-context --file release.yml --repo ObaIsSoft/subway-context --allow-publish
   ```

   You can do the same on the package's **Settings** page on npmjs.com. Do not limit it to the `release` environment, because previews publish without that environment.
3. Optional: on the package **Settings** page on npmjs.com, enable **Require two-factor authentication and disallow tokens**. Trusted publishing still works.

## If a run fails

- **Failed before the npm publish**: nothing is public. Fix the problem and run the workflow again.
- **You rejected a production run, or it failed after the draft was made**: delete the draft release (`gh release delete vX.Y.Z`) before running again, or the next run stops at "GitHub release already exists".
- **npm publish succeeded but a GitHub step failed**: do not re-run the whole job, because it would try to publish again. Run the failed `gh` step by hand, using the command shown in the log. For production, the draft release stays a draft until that step runs.
