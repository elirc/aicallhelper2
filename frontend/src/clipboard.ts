/**
 * Clipboard write with a non-secure-context fallback.
 *
 * The packaged app loads from file://, where `navigator.clipboard` is
 * undefined (it requires a secure context) — without the textarea +
 * execCommand fallback, Copy would fail in the shipped build while passing
 * in dev on http://localhost.
 */
export async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    textarea.remove();
  }
  if (!copied) throw new Error("execCommand copy failed");
}
