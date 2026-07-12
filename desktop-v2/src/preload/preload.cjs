const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("registrationDesk", {
  getState: () => ipcRenderer.invoke("state:get"),
  copyText: (value) => ipcRenderer.invoke("clipboard:write", value),
  importAccounts: (text) => ipcRenderer.invoke("accounts:import", text),
  importSessions: (text, emailHint = "") => ipcRenderer.invoke("sessions:import", text, emailHint),
  importLegacy: () => ipcRenderer.invoke("legacy:import"),
  removeAccounts: (ids) => ipcRenderer.invoke("accounts:remove", ids),
  saveSettings: (settings) => ipcRenderer.invoke("settings:save", settings),
  start: (ids) => ipcRenderer.invoke("registrations:start", ids),
  login: (ids) => ipcRenderer.invoke("sessions:login", ids),
  stop: () => ipcRenderer.invoke("registrations:stop"),
  checkSessions: (ids) => ipcRenderer.invoke("sessions:check", ids),
  checkTrial: (ids) => ipcRenderer.invoke("accounts:trial", ids),
  checkHealth: (ids) => ipcRenderer.invoke("accounts:health", ids),
  recentEmails: (id) => ipcRenderer.invoke("accounts:mail", id),
  getTotp: (id) => ipcRenderer.invoke("accounts:totp", id),
  getEmailCode: (id) => ipcRenderer.invoke("accounts:email-code", id),
  onEvent: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("registration:event", handler);
    return () => ipcRenderer.removeListener("registration:event", handler);
  },
});
