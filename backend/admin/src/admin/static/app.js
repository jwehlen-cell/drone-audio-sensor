function confirmTransition(form, currentState, targetState, extraConfirm) {
    const confirmInput = form.querySelector('input[name="confirm"]');
    const message =
        targetState === "wipe_requested"
            ? `Issue REMOTE WIPE for this device?\n\n` +
              `When the phone next connects, it will receive a wipe ` +
              `command and factory-reset itself. This action is ` +
              `irreversible.\n\nType "WIPE" to confirm.`
            : `Change state from ${currentState} to ${targetState}? ` +
              `This may affect device authentication or audio capture.`;
    if (extraConfirm && targetState === "wipe_requested") {
        const typed = window.prompt(message, "");
        if (typed !== "WIPE") {
            return false;
        }
        confirmInput.value = "yes";
        return true;
    }
    if (extraConfirm) {
        if (!window.confirm(message)) return false;
        confirmInput.value = "yes";
        return true;
    }
    return window.confirm(`Change state ${currentState} → ${targetState}?`);
}
