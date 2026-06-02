using System;
using System.IO;
using System.Reflection;
using System.Xml;

internal static class VistaGuideImport
{
    private static string _logPath;

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 2 && args[0] == "--validate")
            {
                ValidateXml(args[1]);
                Console.WriteLine("Vista guide XML validation passed.");
                return 0;
            }
            if (args.Length != 2)
            {
                Console.Error.WriteLine("Usage: HDHRProxyWMC-VistaGuideImport.exe guide.xml log.txt");
                return 2;
            }

            _logPath = args[1];
            string xmlPath = Path.GetFullPath(args[0]);
            ValidateXml(xmlPath);
            Log("Starting Vista ehepg import: " + xmlPath);

            string ehome = Path.Combine(Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\Windows", "ehome");
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs e)
            {
                string candidate = Path.Combine(ehome, new AssemblyName(e.Name).Name + ".dll");
                return File.Exists(candidate) ? Assembly.LoadFrom(candidate) : null;
            };

            string ehepgPath = Path.Combine(ehome, "ehepg.dll");
            if (!File.Exists(ehepgPath))
                throw new FileNotFoundException("Vista ehepg.dll was not found.", ehepgPath);

            Assembly ehepg = Assembly.LoadFrom(ehepgPath);
            Type security = RequireType(ehepg, "Microsoft.Ehome.Epg.Helper.EpgSecurity");
            Type fileHelper = RequireType(ehepg, "Microsoft.Ehome.Epg.Helper.EpgFileHelper");
            Type managerType = RequireType(ehepg, "Microsoft.Ehome.Epg.Loader.GuideLoadManager");

            string encryptedPath = Path.Combine(Path.GetTempPath(), "hdhrproxy-" + Guid.NewGuid().ToString("N") + ".sdf");
            try
            {
                bool encrypted = (bool)security.GetMethod(
                    "EncryptFile",
                    BindingFlags.Public | BindingFlags.Static
                ).Invoke(null, new object[] { xmlPath, encryptedPath });
                if (!encrypted)
                    throw new InvalidOperationException("Vista EpgSecurity.EncryptFile returned false.");

                string databasePath = (string)fileHelper.GetProperty(
                    "CurrentEpgFile",
                    BindingFlags.Public | BindingFlags.Static
                ).GetValue(null, null);
                if (String.IsNullOrEmpty(databasePath))
                    throw new InvalidOperationException("Vista EpgFileHelper.CurrentEpgFile returned no database path.");

                ConstructorInfo managerCtor = managerType.GetConstructors(
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance
                )[0];
                object manager = managerCtor.Invoke(new object[] { null });
                MethodInfo loadXml = managerType.GetMethod(
                    "LoadXmlFile",
                    BindingFlags.Public | BindingFlags.Instance
                );
                bool loaded = (bool)loadXml.Invoke(manager, new object[] { encryptedPath, databasePath });
                if (!loaded)
                    throw new InvalidOperationException("Vista GuideLoadManager.LoadXmlFile returned false.");

                Log("Vista ehepg import completed successfully.");
                return 0;
            }
            finally
            {
                try { if (File.Exists(encryptedPath)) File.Delete(encryptedPath); } catch { }
            }
        }
        catch (Exception ex)
        {
            Log("FATAL: " + ex);
            Console.Error.WriteLine(ex);
            return 1;
        }
    }

    private static void ValidateXml(string path)
    {
        XmlDocument doc = new XmlDocument();
        doc.Load(path);
        XmlElement root = doc.DocumentElement;
        if (root == null || root.Name != "epg_data")
            throw new InvalidDataException("Vista guide XML must start with epg_data.");

        RequireSection(root, "copyright");
        XmlElement tuneRequests = RequireSection(root, "tuneRequests");
        RequireCount(tuneRequests, "tr");
        RequireSection(root, "categories");
        RequireSection(root, "programAttributes");
        RequireSection(root, "programRatingAttributes");
        RequireSection(root, "scheduleEntryAttributes");
        XmlElement programs = RequireSection(root, "programs");
        RequireCount(programs, "p");
        RequireSection(root, "scheduleEntries");
        RequireSection(root, "scheduleEntries2");
    }

    private static XmlElement RequireSection(XmlElement root, string name)
    {
        XmlElement section = root[name];
        if (section == null)
            throw new InvalidDataException("Vista guide XML is missing section: " + name);
        return section;
    }

    private static void RequireCount(XmlElement section, string childName)
    {
        int expected;
        if (!Int32.TryParse(section.GetAttribute("ct"), out expected))
            throw new InvalidDataException(section.Name + " is missing a valid ct attribute.");
        if (section.GetElementsByTagName(childName).Count != expected)
            throw new InvalidDataException(section.Name + " ct does not match its child count.");
    }

    private static Type RequireType(Assembly assembly, string name)
    {
        Type type = assembly.GetType(name, false);
        if (type == null)
            throw new TypeLoadException("Missing Vista ehepg type: " + name);
        return type;
    }

    private static void Log(string message)
    {
        try
        {
            if (!String.IsNullOrEmpty(_logPath))
                File.AppendAllText(_logPath, "[VistaGuideImport] " + message + Environment.NewLine);
        }
        catch { }
    }
}
