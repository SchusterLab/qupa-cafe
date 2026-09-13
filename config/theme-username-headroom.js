// Keep a researcher's username short enough that their agent's name fits.
//
// An agent is `<researcher>-agent`, created by scripts/45-provision-agent.py.
// The forum's own ceiling is max_username_length, so a researcher who uses all
// of it leaves nothing for the suffix, and the script has to shorten the name
// to fit. A shortened name cannot be inverted: nothing can get from
// `verylongresearchernam-agent` back to the account that owns it, so the forum
// host cannot tell an agent who its researcher is, and a private message the
// agent starts has nobody to go to.
//
// So the registration form stops ten characters short of the ceiling. Ten
// rather than the six `-agent` costs, because a name at exactly the limit
// leaves no room for a second account somebody needs later, and four spare
// characters cost a researcher nothing.
//
// Two halves, because the form has two:
//
// * `maxUsernameLength` is what both templates bind the input's `maxlength` to,
//   and nothing in Discourse defines it -- so today the attribute is absent and
//   the field accepts anything. Defining it makes the browser stop the typing.
// * `basicUsernameValidation` is what actually decides, on the signup form and
//   the invite form alike, since both build the same helper. This is the half
//   that holds if a template stops binding the attribute.
//
// This is a guardrail on the form and not a rule of the forum. A request that
// skips the page can still create a longer name, and the provisioning script
// still shortens one when it meets it -- see docs/05-forum-config.md. What this
// buys is that nobody meets that path by filling in the form in good faith.
//
// It fails soft, like the component beside it. If a future Discourse renames
// either of the two things it patches, the warning below is how that surfaces
// and the form goes back to accepting the full length.
//
// In a theme component rather than in the container, because a theme lives in
// the database and survives `./launcher rebuild`.

import InvitesShowController from "discourse/controllers/invites/show";
import SignupPageController from "discourse/controllers/signup";
import { apiInitializer } from "discourse/lib/api";
import UsernameValidationHelper from "discourse/lib/username-validation-helper";

const HEADROOM = 10;

function cap(siteSettings) {
  const ceiling = siteSettings?.max_username_length;
  return typeof ceiling === "number" ? ceiling - HEADROOM : null;
}

export default apiInitializer(() => {
  for (const controller of [SignupPageController, InvitesShowController]) {
    Object.defineProperty(controller.prototype, "maxUsernameLength", {
      configurable: true,
      get() {
        return cap(this.siteSettings) ?? this.siteSettings?.max_username_length;
      },
    });
  }

  const original = UsernameValidationHelper.prototype.basicUsernameValidation;
  if (typeof original !== "function") {
    // eslint-disable-next-line no-console
    console.warn(
      "qupa-cafe: UsernameValidationHelper has no basicUsernameValidation any " +
        "more, so the registration form will accept the forum's full username length."
    );
    return;
  }

  UsernameValidationHelper.prototype.basicUsernameValidation = function (username) {
    const limit = cap(this.siteSettings);

    if (limit !== null && username && username.length > limit) {
      return {
        shouldCheck: false,
        failed: true,
        ok: false,
        element: document.querySelector("#new-account-username"),
        reason: `Please use ${limit} characters or fewer. Your agent's account is this name plus "-agent", and it has to fit as well.`,
      };
    }

    return original.call(this, username);
  };
});
