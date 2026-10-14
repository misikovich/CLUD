// CLUD.exe - icon-carrying stub that runs CLUD.cmd from its own folder, in its own console.
// Exists only because a .cmd file can't have a custom icon.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;

static class Clud
{
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string cmd = Path.Combine(dir, "CLUD.cmd");
        if (!File.Exists(cmd))
        {
            Console.Error.WriteLine("CLUD.cmd not found next to CLUD.exe");
            Console.ReadKey(true);
            return 1;
        }
        string passed = string.Join(" ", args.Select(a => "\"" + a + "\""));
        var psi = new ProcessStartInfo("cmd.exe", "/c \"\"" + cmd + "\" " + passed + "\"")
        {
            UseShellExecute = false,
            WorkingDirectory = dir
        };
        using (var p = Process.Start(psi))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }
}
