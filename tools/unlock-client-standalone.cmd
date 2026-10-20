<# :
@echo off
rem Unlock the Asheron's Call client so another copy can start: closes the "Empyrean Client" instance lock in the
rem clients that are running. Needs nothing installed (Windows PowerShell, which comes with Windows 10 and 11).
rem Double-click it, or run it from a command prompt. /dry-run lists the locks without closing them.
set "UNLOCK_SELF=%~f0"
set "UNLOCK_ARGS=%*"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:UNLOCK_SELF)))"
set "UNLOCK_EXIT=%ERRORLEVEL%"
echo.
if not defined UNLOCK_NOPAUSE pause
exit /b %UNLOCK_EXIT%
#>
# ---- PowerShell from here (the batch part above is a comment to it) ------------------------------------------------
# The client makes a named semaphore "Empyrean Client" and won't start while one exists ("There is a client already
# running on this machine"). Closing that handle inside each running client removes it; the client only uses it for
# that check. A client you started yourself lets you do this without administrator rights.
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

public static class EmpyreanUnlock {
    const int SystemExtendedHandleInformation = 64, ObjectNameInformation = 1, ObjectTypeInformation = 2;
    const int StatusInfoLengthMismatch = unchecked((int)0xC0000004);
    const int ProcessDupHandle = 0x0040, DuplicateCloseSource = 0x1, DuplicateSameAccess = 0x2;

    [StructLayout(LayoutKind.Sequential)]
    struct HandleEntry {
        public IntPtr Object; public UIntPtr Pid; public UIntPtr Handle;
        public uint Access; public ushort Creator; public ushort TypeIndex; public uint Attributes; public uint Reserved;
    }

    [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int cls, IntPtr buf, int len, out int needed);
    [DllImport("ntdll.dll")] static extern int NtQueryObject(IntPtr h, int cls, IntPtr buf, int len, out int needed);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(int access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool DuplicateHandle(IntPtr srcProc, IntPtr src,
        IntPtr dstProc, out IntPtr dst, int access, bool inherit, int options);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);

    // An object's type or name (a UNICODE_STRING at the start of the answer), or null
    static string Query(IntPtr h, int cls) {
        IntPtr buf = Marshal.AllocHGlobal(0x1000);
        try {
            int needed;
            if (NtQueryObject(h, cls, buf, 0x1000, out needed) != 0) return null;
            int len = Marshal.ReadInt16(buf);
            IntPtr text = Marshal.ReadIntPtr(buf, IntPtr.Size);
            return text == IntPtr.Zero ? "" : Marshal.PtrToStringUni(text, len / 2);
        } finally { Marshal.FreeHGlobal(buf); }
    }

    static List<HandleEntry> AllHandles() {
        int size = 1 << 20;
        while (true) {
            IntPtr buf = Marshal.AllocHGlobal(size);
            try {
                int needed;
                int status = NtQuerySystemInformation(SystemExtendedHandleInformation, buf, size, out needed);
                if (status == StatusInfoLengthMismatch) { size = Math.Max(size * 2, needed + (1 << 16)); continue; }
                if (status != 0) throw new Exception(String.Format("NtQuerySystemInformation failed: 0x{0:X8}", status));
                long count = Marshal.ReadIntPtr(buf).ToInt64();
                int entry = Marshal.SizeOf(typeof(HandleEntry));
                var all = new List<HandleEntry>((int)count);
                for (long i = 0; i < count; i++)
                    all.Add((HandleEntry)Marshal.PtrToStructure(new IntPtr(buf.ToInt64() + 2 * IntPtr.Size + i * entry), typeof(HandleEntry)));
                return all;
            } finally { Marshal.FreeHGlobal(buf); }
        }
    }

    // Close the lock in the processes whose names start with one of the prefixes; how many failed (0: all good)
    public static int Run(string lockName, string[] prefixes, bool dryRun) {
        var procs = new Dictionary<long, Tuple<IntPtr, string>>();
        foreach (var p in Process.GetProcesses())
            foreach (var prefix in prefixes)
                if (p.ProcessName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) {
                    IntPtr h = OpenProcess(ProcessDupHandle, false, p.Id);
                    if (h != IntPtr.Zero) procs[p.Id] = Tuple.Create(h, p.ProcessName + ".exe");
                    else Console.WriteLine("Can't look inside {0}.exe PID {1} (error {2}): if it runs as administrator, run this as administrator too.",
                        p.ProcessName, p.Id, Marshal.GetLastWin32Error());
                    break;
                }
        if (procs.Count == 0) { Console.WriteLine("No client running: nothing to unlock."); return 0; }
        int found = 0, failed = 0;
        IntPtr me = GetCurrentProcess();
        try {
            foreach (var e in AllHandles()) {
                Tuple<IntPtr, string> proc;
                if (!procs.TryGetValue((long)e.Pid.ToUInt64(), out proc)) continue;
                IntPtr dup;
                if (!DuplicateHandle(proc.Item1, new IntPtr((long)e.Handle.ToUInt64()), me, out dup, 0, false, DuplicateSameAccess)) continue;
                string name;
                try {
                    // only semaphores get a name query: on some other kinds (pipes) it can hang
                    if (Query(dup, ObjectTypeInformation) != "Semaphore") continue;
                    name = Query(dup, ObjectNameInformation) ?? "";
                } finally { CloseHandle(dup); }
                if (!name.EndsWith("\\" + lockName)) continue;
                found++;
                if (dryRun) { Console.WriteLine("Would close {0} (handle 0x{1:X}) in {2} PID {3}", name, e.Handle.ToUInt64(), proc.Item2, e.Pid); continue; }
                IntPtr none;
                if (DuplicateHandle(proc.Item1, new IntPtr((long)e.Handle.ToUInt64()), IntPtr.Zero, out none, 0, false, DuplicateCloseSource))
                    Console.WriteLine("Closed the instance lock in {0} PID {1}", proc.Item2, e.Pid);
                else { failed++; Console.WriteLine("Couldn't close the lock in {0} PID {1} (error {2})", proc.Item2, e.Pid, Marshal.GetLastWin32Error()); }
            }
        } finally { foreach (var p in procs.Values) CloseHandle(p.Item1); }
        if (found == 0) Console.WriteLine("No '{0}' lock in the running client(s): already unlocked, start another.", lockName);
        else if (failed == 0 && !dryRun) Console.WriteLine("Unlocked: you can start another client.");
        return failed;
    }
}
"@
$dry = ($env:UNLOCK_ARGS -split '\s+') -contains '/dry-run'
# (the clients: client.exe, client-windows-3d-accel.exe, ...; UNLOCK_PROCESSES and UNLOCK_NAME override, to test)
$names = if ($env:UNLOCK_PROCESSES) { $env:UNLOCK_PROCESSES -split ',' } else { @('client') }
$lock = if ($env:UNLOCK_NAME) { $env:UNLOCK_NAME } else { "Empyrean Client" }
exit [EmpyreanUnlock]::Run($lock, $names, $dry)
