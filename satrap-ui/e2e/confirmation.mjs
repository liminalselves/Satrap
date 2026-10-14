import assert from 'node:assert/strict';

export async function finishConfirmation(page, accepted = true, expectedMessage) {
  const dialog = page.getByRole('dialog', { name: '确认操作', exact: true });
  await dialog.waitFor();
  if (expectedMessage) assert.match(await dialog.innerText(), expectedMessage);
  await dialog.getByRole('button', { name: accepted ? '确认' : '取消', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
}

export function autoAcceptConfirmations(page) {
  let active = true;
  let failure;
  const worker = (async () => {
    while (active && !page.isClosed()) {
      const dialog = page.getByRole('dialog', { name: '确认操作', exact: true });
      try { await dialog.waitFor({ state: 'visible', timeout: 100 }); }
      catch (error) {
        if (!active || page.isClosed()) return;
        if (error.name === 'TimeoutError') continue;
        throw error;
      }
      if (active) await finishConfirmation(page);
    }
  })().catch((error) => { failure = error; });
  return async () => {
    active = false;
    await worker;
    if (failure) throw failure;
  };
}
