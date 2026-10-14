// Scratch scene slots: scene folders that can be filled while the game runs.
//
// Two engine rules make these necessary. A scene folder has to exist when the game starts, or the game cannot load it;
// zero-byte scene.xscene, terrain.bin and navmesh.bin are enough for that. And once a scene has been loaded, the engine
// keeps its terrain.bin open until the game exits, so a folder takes one scene a session.
// `bl slots create` makes the folders (SceneObj/<prefix>_<n>). Take() hands out one that has not been used this session;
// the caller writes the three files into it and loads the scene by the slot's name. Slots filled in a past session are
// emptied again at the next start (EmptyUsed), when nothing is held yet.
using System;
using System.Collections.Generic;
using System.IO;

namespace __MOD__
{
    static class SceneSlots
    {
        static readonly string[] Files = { "scene.xscene", "terrain.bin", "navmesh.bin" };
        static readonly HashSet<string> Used = new HashSet<string>();

        static string Root => Path.Combine(SubModule.ModulePath, "SceneObj");
        public static string Folder(string slot) => Path.Combine(Root, slot);

        // every slot of a prefix, in order: <prefix>_1, <prefix>_2, ...
        public static List<string> All(string prefix)
        {
            var l = new List<string>();
            for (int n = 1; Directory.Exists(Folder(prefix + "_" + n)); n++) l.Add(prefix + "_" + n);
            return l;
        }

        // false while the engine holds the slot's terrain file (it shares the file for reading only)
        public static bool Free(string slot)
        {
            string f = Path.Combine(Folder(slot), "terrain.bin");
            if (!File.Exists(f)) return false;
            try { using (new FileStream(f, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) return true; }
            catch (IOException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
        }

        // the name of a slot not used this session, or null when all are taken; the slot counts as used from now on
        public static string Take(string prefix)
        {
            foreach (string s in All(prefix))
                if (!Used.Contains(s) && Free(s)) { Used.Add(s); return s; }
            Log.Write("slots: none left for " + prefix + " this session");
            return null;
        }

        // at the start of the game: slots a past session filled are emptied (the files stay, with nothing in them)
        public static void EmptyUsed()
        {
            try
            {
                if (!Directory.Exists(Root)) return;
                int emptied = 0;
                foreach (string dir in Directory.GetDirectories(Root))
                {
                    if (!File.Exists(Path.Combine(dir, "slot.txt"))) continue;       // the marker `bl slots create` leaves in a slot
                    foreach (string fn in Files)
                    {
                        string f = Path.Combine(dir, fn);
                        if (File.Exists(f) && new FileInfo(f).Length > 0) { File.WriteAllBytes(f, new byte[0]); emptied++; }
                    }
                }
                if (emptied > 0) Log.Write("slots: " + emptied + " files of slots used last session emptied");
            }
            catch (Exception e) { Log.Write("slots: emptying failed: " + e.Message); }
        }
    }
}
