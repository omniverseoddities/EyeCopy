using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

// Queries Windows USB hub drivers. Does not change drivers, pairing, or phone data.
public static class IPhoneUsbProbe
{
    [StructLayout(LayoutKind.Sequential)]
    private struct InterfaceData { public int Size; public Guid ClassGuid; public int Flags; public IntPtr Reserved; }
    [StructLayout(LayoutKind.Sequential)]
    private struct DeviceData { public int Size; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
    [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern IntPtr SetupDiGetClassDevs(ref Guid guid, string enumerator, IntPtr parent, uint flags);
    [DllImport("setupapi.dll", SetLastError=true)]
    private static extern bool SetupDiEnumDeviceInterfaces(IntPtr list, IntPtr device, ref Guid guid, uint index, ref InterfaceData data);
    [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern bool SetupDiGetDeviceInterfaceDetail(IntPtr list, ref InterfaceData data, IntPtr detail, uint size, out uint needed, ref DeviceData device);
    [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern bool SetupDiGetDeviceInstanceId(IntPtr list, ref DeviceData device, System.Text.StringBuilder id, uint size, out uint needed);
    [DllImport("setupapi.dll", SetLastError=true)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool DeviceIoControl(SafeFileHandle file, uint code, byte[] input, uint inputSize, byte[] output, uint outputSize, out uint returned, IntPtr overlapped);

    public sealed class PortResult
    {
        public string HubInstanceId;
        public int Port;
        public int ConnectionStatus;
        public string VendorId;
        public string ProductId;
        public string DeviceDescriptorUsbVersion;
        public int LegacySpeedCode;
        public string NegotiatedSpeed;
        public string SignalingRate;
        public bool V2QuerySucceeded;
        public uint SupportedProtocolBits;
        public bool OperatingAtSuperSpeedOrHigher;
        public bool OperatingAtSuperSpeedPlusOrHigher;
        public bool SuperSpeedCapable;
        public bool SuperSpeedPlusCapable;
        public int V2Error;
        public bool SuperSpeedPlusDetailsQuerySucceeded;
        public long ReceiveBitsPerSecond;
        public long TransmitBitsPerSecond;
        public uint ReceiveLaneCount;
        public uint TransmitLaneCount;
        public int SuperSpeedPlusDetailsError;
        public int QueryError;
    }
    private static void Put(byte[] buffer, int offset, uint value) { Array.Copy(BitConverter.GetBytes(value), 0, buffer, offset, 4); }
    private static uint Ioctl(uint function) { return (0x22u << 16) | (function << 2); }
    private static long LaneBitsPerSecond(uint attr)
    {
        long mantissa = (attr >> 16) & 0xffff;
        int exponent = (int)((attr >> 4) & 3);
        for(int i=0;i<exponent;i++) mantissa *= 1000;
        return mantissa;
    }
    public static PortResult[] Query(string parentHubInstanceId, int port)
    {
        if(String.IsNullOrWhiteSpace(parentHubInstanceId)) throw new ArgumentException("A USB hub instance ID is required.", "parentHubInstanceId");
        if(port < 1) throw new ArgumentOutOfRangeException("port", "USB ports are numbered from one.");
        var results = new List<PortResult>();
        Guid hubGuid = new Guid("f18a0e88-c30c-11d0-8815-00a0c906bed8");
        IntPtr list = SetupDiGetClassDevs(ref hubGuid, null, IntPtr.Zero, 0x12);
        if(list == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        try
        {
            for(uint i=0;;i++)
            {
                var iface = new InterfaceData { Size = Marshal.SizeOf(typeof(InterfaceData)) };
                if(!SetupDiEnumDeviceInterfaces(list, IntPtr.Zero, ref hubGuid, i, ref iface))
                { if(Marshal.GetLastWin32Error() == 259) break; throw new Win32Exception(Marshal.GetLastWin32Error()); }
                var device = new DeviceData { Size = Marshal.SizeOf(typeof(DeviceData)) };
                uint needed;
                SetupDiGetDeviceInterfaceDetail(list, ref iface, IntPtr.Zero, 0, out needed, ref device);
                IntPtr detail = Marshal.AllocHGlobal((int)needed);
                try
                {
                    Marshal.WriteInt32(detail, IntPtr.Size == 8 ? 8 : 6);
                    if(!SetupDiGetDeviceInterfaceDetail(list, ref iface, detail, needed, out needed, ref device)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    var id = new System.Text.StringBuilder(1024);
                    if(!SetupDiGetDeviceInstanceId(list, ref device, id, 1024, out needed)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    if(!String.Equals(id.ToString(), parentHubInstanceId, StringComparison.OrdinalIgnoreCase)) continue;
                    string path = Marshal.PtrToStringUni(IntPtr.Add(detail, 4));
                    var result = new PortResult { HubInstanceId = parentHubInstanceId, Port = port, NegotiatedSpeed = "Unknown", SignalingRate = "Unknown" };
                    using(var hub = CreateFile(path, 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero))
                    {
                        if(hub.IsInvalid) { result.QueryError = Marshal.GetLastWin32Error(); results.Add(result); continue; }
                        // USB_NODE_CONNECTION_INFORMATION_EX is packed: descriptor=offset 4,
                        // speed=23, connection status=31. Extra space covers open pipe records.
                        var info = new byte[4096]; Put(info,0,(uint)port);
                        uint returned;
                        if(!DeviceIoControl(hub, Ioctl(274), info, (uint)info.Length, info, (uint)info.Length, out returned, IntPtr.Zero))
                        { result.QueryError=Marshal.GetLastWin32Error(); results.Add(result); continue; }
                        if(returned < 35) { result.QueryError = 13; results.Add(result); continue; }
                        result.ConnectionStatus = BitConverter.ToInt32(info,31);
                        result.VendorId = BitConverter.ToUInt16(info,12).ToString("X4");
                        result.ProductId = BitConverter.ToUInt16(info,14).ToString("X4");
                        result.DeviceDescriptorUsbVersion = BitConverter.ToUInt16(info,6).ToString("X4");
                        result.LegacySpeedCode = info[23];
                        var v2 = new byte[16]; Put(v2,0,(uint)port); Put(v2,4,16); Put(v2,8,7);
                        result.V2QuerySucceeded=DeviceIoControl(hub,Ioctl(279),v2,16,v2,16,out returned,IntPtr.Zero);
                        if(result.V2QuerySucceeded && returned < 16) { result.V2QuerySucceeded=false; result.V2Error=13; }
                        if(result.V2QuerySucceeded)
                        {
                            result.SupportedProtocolBits = BitConverter.ToUInt32(v2,8);
                            uint flags = BitConverter.ToUInt32(v2,12);
                            result.OperatingAtSuperSpeedOrHigher = (flags & 1) != 0;
                            result.SuperSpeedCapable = (flags & 2) != 0;
                            result.OperatingAtSuperSpeedPlusOrHigher = (flags & 4) != 0;
                            result.SuperSpeedPlusCapable = (flags & 8) != 0;
                        }
                        else if(result.V2Error==0) result.V2Error=Marshal.GetLastWin32Error();
                        if(result.OperatingAtSuperSpeedPlusOrHigher)
                        {
                            var plus = new byte[24]; Put(plus,0,(uint)port); Put(plus,4,24);
                            result.SuperSpeedPlusDetailsQuerySucceeded = DeviceIoControl(hub,Ioctl(289),plus,24,plus,24,out returned,IntPtr.Zero);
                            if(result.SuperSpeedPlusDetailsQuerySucceeded && returned<24) { result.SuperSpeedPlusDetailsQuerySucceeded=false; result.SuperSpeedPlusDetailsError=13; }
                            if(result.SuperSpeedPlusDetailsQuerySucceeded)
                            {
                                result.ReceiveLaneCount = BitConverter.ToUInt32(plus,12) + 1;
                                result.TransmitLaneCount = BitConverter.ToUInt32(plus,20) + 1;
                                result.ReceiveBitsPerSecond = LaneBitsPerSecond(BitConverter.ToUInt32(plus,8)) * result.ReceiveLaneCount;
                                result.TransmitBitsPerSecond = LaneBitsPerSecond(BitConverter.ToUInt32(plus,16)) * result.TransmitLaneCount;
                            }
                            else if(result.SuperSpeedPlusDetailsError==0) result.SuperSpeedPlusDetailsError = Marshal.GetLastWin32Error();
                        }
                        if(result.ConnectionStatus==1)
                        {
                            if(result.OperatingAtSuperSpeedPlusOrHigher) { result.NegotiatedSpeed="SuperSpeedPlus"; result.SignalingRate="10 Gbit/s or higher (V2 does not identify lane count)"; }
                            else if(result.OperatingAtSuperSpeedOrHigher) { result.NegotiatedSpeed="SuperSpeed"; result.SignalingRate="5 Gbit/s or higher"; }
                            else if(result.LegacySpeedCode==2 && result.V2QuerySucceeded) { result.NegotiatedSpeed="HighSpeed"; result.SignalingRate="480 Mbit/s"; }
                            else if(result.LegacySpeedCode==1) { result.NegotiatedSpeed="FullSpeed"; result.SignalingRate="12 Mbit/s"; }
                            else if(result.LegacySpeedCode==0) { result.NegotiatedSpeed="LowSpeed"; result.SignalingRate="1.5 Mbit/s"; }
                            if(result.SuperSpeedPlusDetailsQuerySucceeded && result.ReceiveBitsPerSecond>0 && result.ReceiveBitsPerSecond==result.TransmitBitsPerSecond)
                                result.SignalingRate = (result.ReceiveBitsPerSecond / 1000000000.0).ToString("0.###", System.Globalization.CultureInfo.InvariantCulture) + " Gbit/s";
                        }
                    }
                    results.Add(result);
                }
                finally { Marshal.FreeHGlobal(detail); }
            }
        }
        finally { SetupDiDestroyDeviceInfoList(list); }
        return results.ToArray();
    }
}
